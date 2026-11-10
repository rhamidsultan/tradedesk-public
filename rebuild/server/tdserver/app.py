"""FastAPI application. Run with `uvicorn --factory tdserver.app:create_app` (settings from TD2_ variables)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .alerts.routes import router as alerts_router
from .console.routes import router as console_router
from .context import AppContext, build_context
from .kite import router as kite_router
from .routes_auth import router as auth_router
from .health import router as health_router
from .strategies.routes import router as strategies_router
from .paper.routes import router as paper_router
from .live.routes import router as live_router
from .strategies.studio import router as studio_router
from .system import router as system_router
from .market import router as market_router
from .research.routes import router as research_router
from .routes_data import rec_router
from .routes_data import router as data_router
from .settings import get_settings
from .status import router as status_router
from .tools.routes import jrouter as journal_router
from .tools.routes import router as tools_router

STATIC = Path(__file__).parent / "console" / "static"


def create_app(ctx: AppContext | None = None) -> FastAPI:
    ctx = ctx or build_context(get_settings())
    prod = not ctx.settings.is_dev
    app = FastAPI(title="TradeDesk server", version=__version__,
                  docs_url=None if prod else "/docs", redoc_url=None, openapi_url=None if prod else "/openapi.json")
    app.state.ctx = ctx
    from fastapi import Depends
    from .access import area_guard
    from .portfolio.routes import router as portfolio_router
    from .portfolio.routes import us_router
    from .users import router as users_router
    # every router that belongs to an area is mounted behind its guard (GET needs view, changes need use)
    areas = {id(r): a for r, a in ((data_router, "market_data"), (rec_router, "market_data"),
                                   (market_router, "market_data"), (research_router, "backtests"),
                                   (studio_router, "research"), (strategies_router, "research"),
                                   (tools_router, "research"), (paper_router, "paper"), (live_router, "live"),
                                   (health_router, "server"), (system_router, "server"),
                                   (portfolio_router, "portfolio"), (us_router, "us"))}
    for r in (auth_router, kite_router, status_router, alerts_router, console_router, data_router, rec_router,
              research_router, market_router, health_router, system_router, studio_router, strategies_router,
              paper_router, live_router, tools_router, journal_router, users_router, portfolio_router, us_router):
        app.include_router(r, dependencies=[Depends(area_guard(areas[id(r)]))] if id(r) in areas else [])
    app.mount("/console/static", StaticFiles(directory=STATIC), name="console-static")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/console", status_code=302)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Cache-Control", "no-store")
        if prod:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # never echo internals (could hold secrets)
        return JSONResponse({"detail": "Something went wrong on the server."}, status_code=500)

    return app

