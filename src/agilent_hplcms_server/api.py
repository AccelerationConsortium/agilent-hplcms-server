"""FastAPI app exposing STATUS_SPEC v1.0 endpoints for the Agilent UPLC-MS sidecar."""

from __future__ import annotations

import argparse
import os
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import Settings, load_settings
from .models import EquipmentStatus, HealthResponse, PROTOCOL_VERSION, ProbeResponse
from .probes import read_signals as _default_read_signals
from .status_builder import (
    EQUIPMENT_ID,
    EQUIPMENT_KIND,
    EQUIPMENT_NAME,
    build_status,
)


SignalReader = Callable[[Settings], dict]


def _adapt(
    reader: Callable[..., dict] = _default_read_signals,
) -> SignalReader:
    """Wrap probe reader so call sites can swap it in tests via monkeypatch."""

    def _call(settings: Settings) -> dict:
        try:
            return reader(settings)
        except TypeError:
            return reader()

    return _call


def create_app(
    settings: Settings | None = None,
    reader: SignalReader | None = None,
) -> FastAPI:
    """Build the FastAPI app. Tests can inject a fake ``reader``."""
    settings = settings or load_settings()
    reader_fn: SignalReader = reader or _adapt()

    app = FastAPI(
        title="Agilent UPLC-MS Status Sidecar",
        version=__version__,
        description=(
            "Read-only STATUS_SPEC v1.0 sidecar for the Agilent UPLC-MS "
            "instrument. /status is side-effect-free."
        ),
    )

    origin = settings.dashboard_origin
    if origin == "*" or not origin:
        allow_origins = ["*"]
        allow_credentials = False
    else:
        allow_origins = [origin]
        allow_credentials = True
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/", response_model=ProbeResponse)
    def probe() -> ProbeResponse:
        return ProbeResponse(
            equipment_id=EQUIPMENT_ID,
            equipment_name=EQUIPMENT_NAME,
            protocol_version=PROTOCOL_VERSION,
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/status", response_model=EquipmentStatus)
    def status() -> EquipmentStatus:
        signals = reader_fn(settings)
        return build_status(signals, settings=settings)

    return app


app = create_app()


def main() -> None:
    """Console entry point: run uvicorn on configured host/port."""
    settings = load_settings()
    parser = argparse.ArgumentParser(
        prog="agilent-hplcms-server-serve",
        description="Run the Agilent UPLC-MS read-only status sidecar.",
    )
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "agilent_hplcms_server.api:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
