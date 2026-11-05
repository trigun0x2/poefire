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
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from poefire import __version__, builds, pob, pob_tree
from poefire.store import Store
from poefire.tracker import RunStatus
from poefire.web import hud, overview, sessions, settings, views
from poefire.web.runtime import AppRuntime

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class LeagueIn(BaseModel):
    league: str


class CampaignStartIn(BaseModel):
    character: str | None = None


class NotesIn(BaseModel):
    notes: str


class OpacityIn(BaseModel):
    percent: int = Field(ge=20, le=100)
    save: bool = True


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
        with store() as s:
            if state.run is not None:
                # loot so far = snapshots already taken during this run (hideout trips)
                run_json = views.run_rows(s, book, [state.run], now)[0].to_json(cpd)
            latest = s.latest_value(rt.league)
        p = rt.prices.status(rt.price_league)
        value = None
        if latest is not None:
            at, value_chaos, rate = latest
            rate = rate or cpd
            value = {"at": at.isoformat(), "chaos": round(value_chaos, 1),
                     "divine": round(value_chaos / rate, 2) if rate else None}  # fmt: skip
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
            "update": rt.update.to_json(),
            "hotkey": rt.hotkey_json(),
            "overlay": rt.overlay_json(),
            "value": value,
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
                "session": rt.session_health(),
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
    def runs(session: int | None = None, limit: int = 500) -> dict[str, Any]:
        """Runs of one session (default: the current one) with its stats."""
        now = rt.now()
        book = rt.prices.book(rt.price_league)
        cpd = book.chaos_per_divine
        with store() as s:
            span = sessions.find(s, session)
            if span is None:
                raise HTTPException(404, "no such session")
            rows = views.run_rows(s, book, sessions.runs_of(s, span), now, rt.values)
        return {
            "chaos_per_divine": cpd,
            "runs": [r.to_json(cpd) for r in rows[: min(limit, 2000)]],
            "session": {
                **views.session_summary(rows, cpd, now),
                **sessions.summary(span, rows, cpd, now),
            },
            # the current session: the last 10 maps; a past one: all of its maps
            "recent": views.recent_maps(
                rows, cpd, n=views.RECENT_MAPS if span.current else max(len(rows), 1)
            ),
            "last_map": views.last_map(rows, cpd),
            "session_run_ids": [r.run.id for r in rows],
        }

    @app.get("/api/sessions")
    def session_history() -> dict[str, Any]:
        book = rt.prices.book(rt.price_league)
        with store() as s:
            return {"chaos_per_divine": book.chaos_per_divine,
                    "sessions": sessions.history(s, book, rt.now(), rt.values)}  # fmt: skip

    @app.post("/api/sessions/new")
    def new_session() -> dict[str, Any]:
        return {"id": rt.new_session()}

    @app.post("/api/sessions/{session_id}/delete")
    def delete_session(session_id: int) -> dict[str, Any]:
        try:
            rt.delete_session(session_id)
        except LookupError:
            raise HTTPException(404, "no such session") from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {"ok": True}

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

    # --- campaign ---

    @app.get("/api/campaign")
    def campaign_overview() -> dict[str, Any]:
        return {**rt.campaign_overview(), "hotkey": rt.hotkey_json()}

    @app.get("/api/campaign/runs/{run_id}")
    def campaign_run(run_id: int) -> dict[str, Any]:
        d = rt.campaign_run(run_id)
        if d is None:
            raise HTTPException(404, "no such campaign run")
        return d

    @app.post("/api/campaign/start")
    def campaign_start(body: CampaignStartIn) -> dict[str, Any]:
        rt.campaign_start(body.character)
        return rt.campaign_overview()

    @app.post("/api/campaign/toggle")
    def campaign_toggle() -> dict[str, Any]:
        """What the hotkey does: start, pause or resume."""
        return {"action": rt.campaign_toggle(), **rt.campaign_overview()}

    @app.post("/api/campaign/pause")
    def campaign_pause() -> dict[str, Any]:
        if not rt.campaign_pause(True):
            raise HTTPException(409, "no running campaign run to pause")
        return rt.campaign_overview()

    @app.post("/api/campaign/resume")
    def campaign_resume() -> dict[str, Any]:
        if not rt.campaign_pause(False):
            raise HTTPException(409, "no paused campaign run")
        return rt.campaign_overview()

    @app.post("/api/campaign/finish")
    def campaign_finish() -> dict[str, Any]:
        if not rt.campaign_end(finished=True):
            raise HTTPException(409, "no campaign run in progress")
        return rt.campaign_overview()

    @app.post("/api/campaign/stop")
    def campaign_stop() -> dict[str, Any]:
        if not rt.campaign_end(finished=False):
            raise HTTPException(409, "no campaign run in progress")
        return rt.campaign_overview()

    @app.post("/api/campaign/runs/{run_id}/notes")
    def campaign_notes(run_id: int, body: NotesIn) -> dict[str, Any]:
        if len(body.notes) > 20000:
            raise HTTPException(422, "notes are too long")
        if not rt.campaign_notes(run_id, body.notes):
            raise HTTPException(404, "no such campaign run")
        return {"ok": True}

    @app.post("/api/campaign/runs/{run_id}/delete")
    def campaign_delete(run_id: int) -> dict[str, Any]:
        if not rt.campaign_delete(run_id):
            raise HTTPException(404, "no such campaign run")
        return {"ok": True}

    @app.get("/api/overlay")
    def overlay_data() -> dict[str, Any]:
        """What the in-game overlay shows (campaign run, or the mapping session)."""
        with store() as s:
            return hud.overlay_state(rt, s)

    @app.post("/api/overlay/opacity")
    def overlay_opacity(body: OpacityIn) -> dict[str, Any]:
        rt.set_overlay_opacity(body.percent, body.save)
        return {"ok": True}

    # --- build snapshots ---

    @app.get("/api/builds")
    def builds_overview() -> dict[str, Any]:
        with store() as s:
            chars = s.build_characters()
        return {
            "characters": chars,
            "enabled": rt.cfg.build_snapshots,
            "connected": rt._build_api() is not None,
            "reason": None if rt._build_api() is not None else rt.loot.reason,
            "pending": [{"character": c, "level": lv} for c, (lv, _) in rt.builds.pending.items()],
            "last_error": rt.builds.last_error,
        }

    @app.get("/api/builds/character/{character}")
    def build_levels(character: str) -> dict[str, Any]:
        with store() as s:
            snaps = s.list_build_snapshots(character)
        return {"character": character, "snapshots": [
            {"id": b.id, "level": b.level, "taken_at": b.taken_at.isoformat(), "ok": b.ok,
             "error": b.error}
            for b in snaps
        ]}  # fmt: skip

    @app.get("/api/builds/snapshot/{snapshot_id}")
    def build_snapshot(snapshot_id: int) -> dict[str, Any]:
        with store() as s:
            snap = s.get_build_snapshot(snapshot_id)
        if snap is None:
            raise HTTPException(404, "no such build snapshot")
        return builds.summarize(snap)

    @app.get("/api/builds/snapshot/{snapshot_id}/pob")
    def build_pob(snapshot_id: int) -> dict[str, Any]:
        """The snapshot as a Path of Building code (paste into PoB: Import/Export Build)."""
        with store() as s:
            snap = s.get_build_snapshot(snapshot_id)
        if snap is None:
            raise HTTPException(404, "no such build snapshot")
        if snap.data is None:
            raise HTTPException(409, "this level was missed; there's no build to export")
        return {"code": pob.pob_code(snap.data, snap.level), "tree_version": pob_tree.TREE_VERSION}

    @app.post("/api/builds/character/{character}/snapshot")
    def build_snapshot_now(character: str) -> dict[str, Any]:
        if rt._build_api() is None:
            raise HTTPException(409, "build snapshots need the account connection (Settings)")
        rt.builds.request(character)
        return {"ok": True}

    @app.post("/api/builds/snapshot/{snapshot_id}/delete")
    def build_snapshot_delete(snapshot_id: int) -> dict[str, Any]:
        with store() as s:
            if not s.delete_build_snapshot(snapshot_id):
                raise HTTPException(404, "no such build snapshot")
        return {"ok": True}

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

    # --- overview ---

    @app.get("/api/overview/history")
    def overview_history(range: str = "7d") -> dict[str, Any]:
        book = rt.prices.book(rt.price_league)
        with store() as s:
            return overview.history(s, rt.league, range, rt.now(), book.chaos_per_divine)

    @app.get("/api/overview/items")
    def overview_items(tabs: str = "") -> dict[str, Any]:
        book = rt.prices.book(rt.price_league)
        with store() as s:
            return {
                **overview.holdings(s, book, tabs.split(","), cache=rt.values),
                "loot_enabled": rt.loot.enabled,
            }

    # --- updates ---

    @app.get("/api/update")
    def update_state() -> dict[str, Any]:
        return rt.update.to_json()

    @app.post("/api/update/check")
    def update_check() -> dict[str, Any]:
        return rt.check_update().to_json()

    @app.post("/api/update/install")
    def update_install() -> dict[str, Any]:
        if rt.update.available is None:
            raise HTTPException(409, "no update available")
        if not rt.update.can_install:
            raise HTTPException(409, rt.update.cannot_install_reason or "can't self-update")
        rt.install_update()
        return rt.update.to_json()

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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
