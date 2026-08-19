"""Reader for Agilent instrument-curve traces archived inside a ``.dx`` run file.

A completed run writes ``<run>.dx`` into its ``*.sirslt`` directory. The ``.dx``
is an OPC (ZIP) container; each acquired signal is one member, named by its
GUID, holding a ChemStation-derived binary trace. That is where the pump
pressure for every run already lives — no instrument connection, no .NET, no
vendor SDK, just a ZIP member and ``struct``.

File format (version ``179``, the one this instrument writes)
-------------------------------------------------------------
Offsets are fixed; strings are length-prefixed UTF-16LE (one length byte, in
characters, immediately before the text)::

    0x0000  b"\\x03179"       format version marker
    0x035A  sample name
    0x0957  acquisition date, e.g. "11-Aug-26, 10:31:13"
    0x0A0E  acquisition method path
    0x1075  signal description, e.g. "PMP1B,Pressure"
    0x127C  scale factor, ONE BIG-endian float64
    0x1800  data section: records of TWO LITTLE-endian float64,
            (time_milliseconds, raw_value)

The physical value is ``raw_value * scale_factor``. The scale factor is
per-signal and is the only thing that makes the raw counts meaningful — the
container carries no unit string anywhere. It was validated against values known
independently for this instrument: ``LCMS1I`` decodes to 325.0 °C against a live
sensor-daemon reading of 325.0, ``PMP1C`` to 0.50 mL/min against a method flow of
0.5, ``PMP1D/E`` to 5.0/95.0 % against a 5→95 gradient, and ``THM1A/B`` to
40.00 °C against the column thermostat setpoint.

Signal names follow ChemStation convention — ``PMP1B`` is pump 1 channel B
(pressure), ``PMP1C`` flow, ``THM1A`` column thermostat left temperature,
``LCMS1B`` MS high vacuum.
"""

from __future__ import annotations

import logging
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_VERSION_MARKER = b"\x03179"

_OFF_SAMPLE_NAME = 0x035A
_OFF_DATE = 0x0957
_OFF_METHOD = 0x0A0E
_OFF_DESCRIPTION = 0x1075
_OFF_SCALE = 0x127C
_OFF_DATA = 0x1800

# One (time, value) record: two little-endian float64.
_RECORD_SIZE = 16

# Enough to cover every header field, so a signal can be identified without
# pulling the whole member out of the archive.
_HEADER_BYTES = _OFF_DATA


@dataclass(frozen=True)
class Trace:
    """One decoded instrument curve."""

    name: str           # e.g. "PMP1B"
    description: str    # e.g. "PMP1B,Pressure"
    sample_name: str
    method: str
    times_s: list[float]
    values: list[float]  # already scaled into physical units

    @property
    def duration_s(self) -> float:
        if not self.times_s:
            return 0.0
        return self.times_s[-1] - self.times_s[0]


def _pstring(buf: bytes, offset: int) -> str:
    """Read a length-prefixed UTF-16LE string (length byte at ``offset``)."""
    try:
        n_chars = buf[offset]
    except IndexError:
        return ""
    if not n_chars:
        return ""
    start = offset + 1
    raw = buf[start : start + n_chars * 2]
    try:
        return raw.decode("utf-16-le").strip("\x00").strip()
    except UnicodeDecodeError:
        return ""


def _looks_like_trace(header: bytes) -> bool:
    return header[:4] == _VERSION_MARKER


def _description(header: bytes) -> str:
    return _pstring(header, _OFF_DESCRIPTION)


def read_trace(dx_path: str | Path, signal_name: str) -> Trace | None:
    """Return one decoded trace from a ``.dx`` archive, or None if absent.

    ``signal_name`` is matched against the leading comma-separated field of each
    member's description, so ``"PMP1B"`` selects ``"PMP1B,Pressure"``. Members
    are identified by GUID rather than name, so every candidate's header is read
    (a few KB each) until the description matches; only the matching member is
    pulled out in full.

    Returns None — never raises — for a missing, truncated, or unreadable
    archive: a run whose pressure we cannot read is simply a run we say nothing
    about.
    """
    path = Path(dx_path)
    try:
        with zipfile.ZipFile(path) as archive:
            members = [
                name
                for name in archive.namelist()
                if name.upper().endswith((".IT", ".CH"))
            ]
            for member in members:
                try:
                    with archive.open(member) as fh:
                        header = fh.read(_HEADER_BYTES)
                except (OSError, zipfile.BadZipFile):
                    continue
                if len(header) < _HEADER_BYTES or not _looks_like_trace(header):
                    continue
                description = _description(header)
                if description.split(",", 1)[0].strip() != signal_name:
                    continue
                return _decode(archive.read(member), description)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        logger.debug("dx_trace: cannot read %s: %s", path, exc)
        return None
    return None


def _decode(buf: bytes, description: str) -> Trace | None:
    if len(buf) <= _OFF_DATA:
        return None
    try:
        (scale,) = struct.unpack_from(">d", buf, _OFF_SCALE)
    except struct.error:
        return None
    if not scale:
        # A zero scale would flatten the whole trace to 0; treat as unusable
        # rather than silently reporting a dead-flat signal.
        return None

    n_records = (len(buf) - _OFF_DATA) // _RECORD_SIZE
    if n_records <= 0:
        return None
    try:
        flat = struct.unpack_from("<%dd" % (2 * n_records), buf, _OFF_DATA)
    except struct.error:
        return None

    times_s = [t / 1000.0 for t in flat[0::2]]
    values = [v * scale for v in flat[1::2]]
    return Trace(
        name=description.split(",", 1)[0].strip(),
        description=description,
        sample_name=_pstring(buf, _OFF_SAMPLE_NAME),
        method=_pstring(buf, _OFF_METHOD),
        times_s=times_s,
        values=values,
    )


def list_signals(dx_path: str | Path) -> list[str]:
    """Return the descriptions of every trace in an archive (diagnostic helper)."""
    out: list[str] = []
    try:
        with zipfile.ZipFile(Path(dx_path)) as archive:
            for member in archive.namelist():
                if not member.upper().endswith((".IT", ".CH")):
                    continue
                with archive.open(member) as fh:
                    header = fh.read(_HEADER_BYTES)
                if len(header) >= _HEADER_BYTES and _looks_like_trace(header):
                    out.append(_description(header))
    except (OSError, zipfile.BadZipFile) as exc:
        logger.debug("dx_trace: cannot list %s: %s", dx_path, exc)
    return sorted(out)
