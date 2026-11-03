"""Local web UI: JSON API + static front end. Binds to localhost only.

Two guards because any website you visit can send requests to localhost:
- the Host header must be localhost/127.0.0.1 (blocks DNS-rebinding reads);
- state-changing POSTs need an `X-Poefire` header, which a cross-site page can't add
  without a CORS preflight that this server never approves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from poefire import __version__
from poefire.store import Store
from poefire.tracker import RunStatus
from poefire.web import settings, views
from poefire.web.runtime import SESSION_GAP, AppRuntime

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class LeagueIn(BaseModel):
    league: str


class LocalOnly(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS and host != "testserver":
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method == "POST" and request.headers.get("x-poefire") != "1":
            return JSONResponse({"detail": "missing X-Poefire header"}, status_code=403)
        return await call_next(request)


def create_app(rt: AppRuntime) -> FastAPI:
    app = FastAPI(title="poefire", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(LocalOnly)

    def store() -> Store:
        return Store(rt.cfg.db_path)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        now = rt.now()
        state = rt.state()
        book = rt.prices.book(rt.price_league)
        cpd = book.chaos_per_divine
        run_json = None
        if state.run is not None:
            # loot so far = snapshots already taken during this run (hideout trips)
            with store() as s:
                run_json = views.run_rows(s, book, [state.run], now)[0].to_json(cpd)
        p = rt.prices.status(rt.price_league)
        last = rt.loot.last
        return {
            "now": now.isoformat(),
            "league": rt.league,
            "log_path": str(rt.log_path) if rt.log_path else None,
            "zone": {"name": state.current.name, "kind": state.current.kind.value}
            if state.current
            else None,
            "in_run": state.in_run,
            "run": run_json,
            "league_is_private": rt.is_private,
            "prices": {
                "league": rt.price_league,
                "entries": p.entries,
                "fetched_at": p.fetched_at,
                "refreshing": p.refreshing,
                "error": p.last_error,
                "failed": p.failed_types,
                "chaos_per_divine": cpd,
            },
            "loot": {
                "enabled": rt.loot.enabled,
                "reason": rt.loot.reason,
                "last": None
                if last is None
                else {
                    "at": rt.loot.last_at.isoformat() if rt.loot.last_at else None,
                    "trigger": last.trigger.value,
                    "complete": last.complete,
                    "error": last.error,
                },
            },
        }

    @app.get("/api/runs")
    def runs(limit: int = 50) -> dict[str, Any]:
        now = rt.now()
        book = rt.prices.book(rt.price_league)
        cpd = book.chaos_per_divine
        with store() as s:
            rows = views.run_rows(s, book, s.list_runs(limit=min(limit, 500)), now)
        session = views.current_session(rows, SESSION_GAP)
        return {
            "chaos_per_divine": cpd,
            "runs": [r.to_json(cpd) for r in rows],
            "session": views.session_summary(session, cpd, now),
            "recent": views.recent_maps(rows, cpd),
            "last_map": views.last_map(rows, cpd),
            "session_run_ids": [r.run.id for r in session],
        }

    @app.get("/api/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        book = rt.prices.book(rt.price_league)
        with store() as s:
            r = s.get_run(run_id)
            if r is None:
                raise HTTPException(404, "no such run")
            live = rt.state().run
            if live is not None and live.id == run_id and live.status is RunStatus.ACTIVE:
                r = live
            return views.run_detail(s, book, r, rt.now())

    @app.get("/api/leagues")
    def leagues() -> dict[str, Any]:
        return {"current": rt.league, "options": rt.leagues()}

    @app.post("/api/league")
    def set_league(body: LeagueIn) -> dict[str, Any]:
        name = body.league.strip()
        if not name or len(name) > 100:
            raise HTTPException(400, "bad league name")
        rt.set_league(name)
        return {"current": rt.league}

    @app.post("/api/prices/refresh")
    def refresh_prices() -> dict[str, Any]:
        rt.refresh_prices(force=True)
        return {"ok": True}

    @app.post("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        if not rt.snapshot_now():
            raise HTTPException(409, rt.loot.reason or "loot tracking is off")
        return {"ok": True}

    # --- settings ---

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings.settings_json(rt)

    @app.post("/api/settings")
    def post_settings(body: settings.SettingsIn) -> dict[str, Any]:
        try:
            warnings = settings.update_settings(rt, body)
        except settings.SettingsError as e:
            raise HTTPException(400, str(e)) from e
        return {**settings.settings_json(rt), "warnings": warnings}

    @app.post("/api/settings/session")
    def post_session(body: settings.SessionIn) -> dict[str, Any]:
        try:
            warnings = settings.save_session(rt, body.poesessid)
        except settings.SettingsError as e:
            raise HTTPException(400, str(e)) from e
        return {**settings.settings_json(rt), "warnings": warnings}

    @app.post("/api/settings/session/clear")
    def clear_session() -> dict[str, Any]:
        settings.clear_session(rt)
        return settings.settings_json(rt)

    @app.post("/api/settings/check-account")
    def check_account() -> dict[str, Any]:
        return settings.check_account(rt)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
