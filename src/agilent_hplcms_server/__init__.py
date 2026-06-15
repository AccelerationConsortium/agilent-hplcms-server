"""Read-only STATUS_SPEC v1.0 sidecar for the Agilent UPLC-MS instrument.

This package observes the existing `moses` controller and the Agilent OpenLab
CDS supervisor without touching them. It never imports `moses`, never loads
`pythonnet`, and never opens an instrument session.
"""

__version__ = "0.2.0"
