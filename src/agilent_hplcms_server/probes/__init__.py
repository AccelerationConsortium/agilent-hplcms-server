"""Read-only probes used by the sidecar.

Each probe module exposes a single pure function ``read_signals() -> dict``
populated with optional values. Probes never open a session against the
instrument, never import the existing controller package, and never mutate any
external file. The default probe for v0.1 is :mod:`.process`.
"""

from .process import read_signals

__all__ = ["read_signals"]
