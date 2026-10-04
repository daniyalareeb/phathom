"""FastAPI app factory + runner (EXTENSION_SPEC §6). Binds 127.0.0.1 only."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from phathom.logging_setup import setup_logging
from phathom.server.api import router
from phathom.server.auth import get_or_create_token, origin_allowed
from phathom.server.ws import ExtensionManager, websocket_endpoint
from phathom.store import close_shared

log = logging.getLogger("phathom.server")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    # Embedded DB: flush + release the lock so the CLI can use it after Ctrl+C.
    await close_shared()


def create_app(manager: ExtensionManager | None = None) -> FastAPI:
    app = FastAPI(title="phathom", lifespan=_lifespan)
    app.state.manager = manager or ExtensionManager()

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if not origin_allowed(origin):
            return JSONResponse(status_code=403, content={"detail": "bad origin"})
        return await call_next(request)

    app.include_router(router)

    @app.get("/health")
    async def root_health():
        return {"server": "phathom", "ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        # Same Origin rule for sockets: reject a present-but-wrong origin.
        origin = ws.headers.get("origin")
        if not origin_allowed(origin):
            await ws.close(code=4403)
            return
        await websocket_endpoint(ws, app.state.manager)

    return app


app = create_app()


def run(port: int = 8765) -> None:
    import uvicorn
    setup_logging()
    token = get_or_create_token()
    try:
        from phathom.server.auth import EXT_ID_PATH, pinned_extension_id
        _ = EXT_ID_PATH
    except Exception:  # noqa: BLE001
        pass
    from phathom.config import settings
    from phathom.store import is_embedded
    log.info("phathom serve on 127.0.0.1:%d (ext id pinned: %s)", port, pinned_extension_id())
    log.info("database: %s%s", settings.SURREAL_URL, " (embedded)" if is_embedded(settings.SURREAL_URL) else "")
    log.info("extension token: paste once via `phathom token` (stored in data/server_token)")
    log.info("Phathom is running. Press Ctrl+C to stop.")
    _ = token
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
