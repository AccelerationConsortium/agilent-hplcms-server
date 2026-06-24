"""FastAPI app exposing STATUS_SPEC v1.0 endpoints for the Agilent UPLC-MS sidecar."""

from __future__ import annotations

import argparse
import os
from typing import Callable

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import Settings, load_settings
from .control import MosesRunner, RosterProvider, router as control_router
from .control.claims import ClaimHolder
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
    runner: MosesRunner | None = None,
    claims: ClaimHolder | None = None,
    roster: RosterProvider | None = None,
) -> FastAPI:
    """Build the FastAPI app. Tests can inject a fake ``reader`` or ``runner``."""
    settings = settings or load_settings()
    reader_fn: SignalReader = reader or _adapt()
    runner_instance = runner if runner is not None else MosesRunner()
    claims_instance = claims if claims is not None else ClaimHolder()
    roster_instance = roster if roster is not None else RosterProvider()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        runner_instance.start_poller(settings=settings)
        roster_instance.start_poller(settings=settings)
        yield
        roster_instance.stop_poller()
        runner_instance.stop_poller()

    app = FastAPI(
        lifespan=lifespan,
        title="Agilent UPLC-MS Status Sidecar",
        version=__version__,
        description=(
            "STATUS_SPEC v1.0 sidecar for the Agilent UPLC-MS instrument. "
            "/status is side-effect-free. /control/* endpoints drive Moses."
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
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    # Store shared state so routers can access it via request.app.state
    app.state.runner = runner_instance
    app.state.settings = settings
    app.state.reader = reader_fn
    app.state.claims = claims_instance
    app.state.roster = roster_instance

    app.include_router(control_router)

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
        return build_status(
            signals, settings=settings, runner=runner_instance, claims=claims_instance
        )

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
