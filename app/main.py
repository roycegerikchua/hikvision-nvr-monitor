from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import load_config
from .monitor import MonitorService
from .repository import load_config_from_sql_env, SqlServerRepository
from .time_sync import clock_check_all, sync_all


def serialize_status(status) -> dict[str, Any]:
    data = status.__dict__.copy()
    for key in ("last_start_time", "last_end_time"):
        if data[key] is not None:
            data[key] = data[key].isoformat()
    return data


def build_app(config_path: str | None = None) -> FastAPI:
    if os.getenv("NVR_CONFIG_SOURCE", "json").lower() in {"sql", "sqlserver", "mssql"}:
        config, repository = load_config_from_sql_env()
    else:
        config = load_config(config_path)
        repository = None
    service = MonitorService(config, result_repository=repository, config_repository=repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.monitor = service
        app.state.repository = repository
        # Start background polling immediately
        task = asyncio.create_task(service.run_forever())
        try:
            yield
        finally:
            service.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Hikvision NVR Recording Monitor", lifespan=lifespan)

    # ── Camera Status ─────────────────────────────────────────
    @app.get("/api/status")
    async def api_status():
        statuses = [serialize_status(item) for item in service.snapshot().values()]
        statuses.sort(key=lambda item: (item["nvr_name"], int(item["camera_id"])))
        return {
            "overall_status": service.overall_status(),
            "last_poll_started_at": service.last_poll_started_at.isoformat() if service.last_poll_started_at else None,
            "last_poll_finished_at": service.last_poll_finished_at.isoformat() if service.last_poll_finished_at else None,
            "stale_after_minutes": config.stale_after_minutes,
            "poll_interval_seconds": config.poll_interval_seconds,
            "cameras": statuses,
        }

    @app.post("/api/poll-now")
    async def poll_now():
        await service.poll_once()
        return await api_status()

    @app.get("/health")
    async def health():
        return {"ok": True, "overall_status": service.overall_status()}

    # ── NVR Clock Check & Sync (must be BEFORE {nvr_id} route) ──
    @app.get("/api/nvrs/clock-check")
    async def nvr_clock_check():
        """Check clock offset on all NVRs via ISAPI."""
        repo = get_repo()
        raw_nvrs = []
        for n in repo.list_nvrs():
            raw = repo.get_nvr(n["nvr_id"])
            if raw:
                raw_nvrs.append(raw)
        results = await clock_check_all(raw_nvrs)
        return {
            "total": len(raw_nvrs),
            "checked": sum(1 for r in results if r["status"] == "ok"),
            "unreachable": sum(1 for r in results if r["status"] != "ok"),
            "results": results,
        }

    @app.post("/api/nvrs/sync-time")
    async def nvr_sync_time():
        """Sync clocks of all reachable NVRs to current PHT time."""
        repo = get_repo()
        raw_nvrs = []
        for n in repo.list_nvrs():
            raw = repo.get_nvr(n["nvr_id"])
            if raw:
                raw_nvrs.append(raw)
        results = await sync_all(raw_nvrs)
        return {
            "total": len(raw_nvrs),
            "synced": sum(1 for r in results if r["status"] == "ok"),
            "failed": sum(1 for r in results if r["status"] == "error"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "results": results,
        }

    # ── NVR Management CRUD ───────────────────────────────────
    @app.get("/api/nvrs")
    async def list_nvrs():
        repo = get_repo()
        return repo.list_nvrs()

    @app.get("/api/nvrs/{nvr_id}")
    async def get_nvr(nvr_id: int):
        repo = get_repo()
        nvr = repo.get_nvr(nvr_id)
        if not nvr:
            raise HTTPException(404, "NVR not found")
        return nvr

    @app.post("/api/nvrs")
    async def add_nvr(body: dict):
        repo = get_repo()
        nvr_id = repo.add_nvr(
            location=body.get("location", ""),
            ip=body.get("ip", ""),
            username=body.get("username", ""),
            password=body.get("password", ""),
            port=int(body.get("port", 80)),
        )
        return {"nvr_id": nvr_id, "message": "NVR added"}

    @app.put("/api/nvrs/{nvr_id}")
    async def update_nvr(nvr_id: int, body: dict):
        repo = get_repo()
        ok = repo.update_nvr(
            nvr_id=nvr_id,
            location=body.get("location", ""),
            ip=body.get("ip", ""),
            username=body.get("username", ""),
            password=body.get("password", ""),
            port=int(body.get("port", 80)),
        )
        if not ok:
            raise HTTPException(404, "NVR not found")
        return {"message": "NVR updated"}

    @app.delete("/api/nvrs/{nvr_id}")
    async def delete_nvr(nvr_id: int):
        repo = get_repo()
        ok = repo.delete_nvr(nvr_id)
        if not ok:
            raise HTTPException(404, "NVR not found")
        return {"message": "NVR deleted"}

    # ── Reload Config from DB ────────────────────────────────
    @app.post("/api/reload-config")
    async def reload_config():
        """Rescan the NVRTest.dbo.NVR table and re-initialise the monitor with fresh config."""
        repo = get_repo()
        new_config = repo.load_config()
        service.config = new_config
        # Reset camera statuses — they'll be rebuilt on the next poll
        service._statuses = {}
        # Optionally trigger an immediate poll
        await service.poll_once()
        return {"message": "Config reloaded from DB", "nvr_count": len(new_config.nvrs)}

    def get_repo() -> SqlServerRepository:
        if repository is None:
            raise HTTPException(400, "SQL Server repository not configured (NVR_CONFIG_SOURCE not set to sql)")
        return repository

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return HTMLResponse(DASHBOARD_HTML)

    return app


DASHBOARD_HTML = Path("/app/app/dashboard.html").read_text()

app = build_app(os.getenv("HIKVISION_MONITOR_CONFIG"))
