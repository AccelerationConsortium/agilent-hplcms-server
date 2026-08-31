r"""Read the multisampler's hotel state and print it per holder.

Why this exists
---------------
``LIST "HOTEL_STATE"`` is the only per-drawer signal the instrument emits, and
two things about it are unverified:

* it returns one entry per physical drawer (``[n, ?, ?, a, b]``), and the two
  half-fields ``a``/``b`` are believed to be the drawer's front and back -- the
  D#F / D#B split sample addresses use -- but which is which is NOT confirmed;
* those fields are tri-state (0/1/2). 0 is empty; 1 and 2 are both non-empty,
  and the difference between them is unknown. They change within seconds during
  acquisition, so they may distinguish "loaded" from "in transport".

Until both are pinned down, nothing safety-critical can key off this signal.
This tool exists to pin them down at the instrument: print the current state,
or ``--watch`` it while you physically add and remove a plate, and read off what
each field does.

Usage (PowerShell, on the instrument PC):

    # current state, decoded:
    uv run python tools/hotel_state_probe.py

    # watch for changes while you load/unload a drawer:
    uv run python tools/hotel_state_probe.py --watch

    # include the raw log line each reading came from:
    uv run python tools/hotel_state_probe.py --raw

Note: HOTEL_STATE is written during prerun/operation, not continuously. If the
state looks stale, trigger a refresh at the instrument (e.g. "Get System Ready")
and the driver logs a fresh reading.

Stdlib only - runs with any Python on the PC.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time
from datetime import datetime

DEFAULT_LOG_DIR = os.environ.get(
    "RC_DRIVER_LOG_DIR", r"C:\ProgramData\Agilent\LogFiles\LC Drivers"
)

_TS_RE = re.compile(r"Timestamp: (\d{2}-\d{2}-\d{4} \d{1,2}:\d{2}:\d{2}[.,]\d+)")
_HOTEL_RE = re.compile(
    r"HOTEL_STATE: \[HOTEL_STATE: ((?:\[\d+(?:,\d+)+\],? *)+)\]\]"
)
_ENTRY_RE = re.compile(r"\[(\d+),(\d+),(\d+),(\d+),(\d+)\]")

# What each half-field value is currently believed to mean. 1 vs 2 is exactly
# what this tool is meant to resolve, so both are reported as "not empty".
_CODE = {0: "empty", 1: "occupied?", 2: "occupied?"}


def _parse_ts(line: str) -> datetime | None:
    m = _TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).replace(",", "."), "%d-%m-%Y %H:%M:%S.%f")
    except ValueError:
        return None


def latest_reading(log_dir: str) -> tuple[datetime, list[tuple[int, ...]], str] | None:
    """Newest HOTEL_STATE across every rotated log: (timestamp, entries, line)."""
    best: tuple[datetime, list[tuple[int, ...]], str] | None = None
    for path in glob.glob(os.path.join(log_dir, "*RCDriver*.log")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            m = _HOTEL_RE.search(line)
            if not m:
                continue
            ts = _parse_ts(line)
            if ts is None:
                continue
            if best is None or ts > best[0]:
                entries = [
                    tuple(int(x) for x in e) for e in _ENTRY_RE.findall(m.group(1))
                ]
                best = (ts, entries, line)
    return best


def render(ts: datetime, entries: list[tuple[int, ...]], show_raw: str | None) -> str:
    out = [f"HOTEL_STATE @ {ts:%Y-%m-%d %H:%M:%S}   ({len(entries) * 2} holders)"]
    out.append("")
    out.append("  holder   field  value  reading")
    out.append("  " + "-" * 42)
    for e in entries:
        drawer = e[0]
        # Field 3 is ASSUMED front, field 4 back. Confirm before relying on it:
        # load a plate into a known half and see which field moves.
        for half, idx in (("F", 3), ("B", 4)):
            val = e[idx]
            out.append(
                f"  D{drawer}{half}      [{idx}]    {val}      {_CODE.get(val, '?')}"
            )
    occupied = sum(1 for e in entries for i in (3, 4) if e[i] != 0)
    out.append("  " + "-" * 42)
    out.append(f"  non-empty: {occupied} of {len(entries) * 2}")
    out.append("")
    out.append("  front/back assignment is UNVERIFIED -- confirm by loading a known half.")
    if show_raw:
        out.append("")
        out.append(f"  raw: {show_raw.strip()[:300]}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                    help=f"RC driver log directory (default: {DEFAULT_LOG_DIR}).")
    ap.add_argument("--watch", action="store_true",
                    help="Poll and print whenever the state changes (Ctrl-C to stop).")
    ap.add_argument("--interval", type=float, default=3.0,
                    help="Seconds between polls in --watch mode (default: 3).")
    ap.add_argument("--raw", action="store_true",
                    help="Also print the raw log line each reading came from.")
    args = ap.parse_args()

    if not os.path.isdir(args.log_dir):
        print(f"error: log directory not found: {args.log_dir}", file=sys.stderr)
        return 2

    reading = latest_reading(args.log_dir)
    if reading is None:
        print(f"error: no HOTEL_STATE reading found under {args.log_dir}", file=sys.stderr)
        print("hint: it is logged during prerun/operation -- trigger 'Get System "
              "Ready' at the instrument, then retry.", file=sys.stderr)
        return 2

    ts, entries, line = reading
    print(render(ts, entries, line if args.raw else None))

    if not args.watch:
        return 0

    print("\nwatching for changes -- load or unload a drawer now (Ctrl-C to stop)\n")
    last = entries
    try:
        while True:
            time.sleep(args.interval)
            nxt = latest_reading(args.log_dir)
            if nxt is None:
                continue
            ts2, entries2, line2 = nxt
            if entries2 != last:
                print("=" * 46)
                print(render(ts2, entries2, line2 if args.raw else None))
                changed = [
                    f"D{a[0]}{h}: {a[i]} -> {b[i]}"
                    for a, b in zip(last, entries2)
                    for h, i in (("F", 3), ("B", 4))
                    if a[i] != b[i]
                ]
                if changed:
                    print("  CHANGED: " + ", ".join(changed))
                last = entries2
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
