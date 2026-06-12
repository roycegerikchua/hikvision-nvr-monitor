from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import load_config
from .monitor import MonitorService
from .repository import load_config_from_sql_env, SqlServerRepository
from .time_sync import clock_check_all, sync_all
from .channel_verify import verify_all_channels, load_inactive, save_inactive


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

    # ── Channel Verification ────────────────────────────────
    @app.post("/api/cameras/verify-channels")
    async def verify_channels():
        """Scan all NVRs' ISAPI channel lists and flag orphaned cameras."""
        repo = get_repo()
        raw_nvrs = []
        for n in repo.list_nvrs():
            raw = repo.get_nvr(n["nvr_id"])
            if raw:
                raw_nvrs.append(raw)
        result = await verify_all_channels(raw_nvrs)
        # Save inactive list
        inactive_set = {(item["nvr_id"], item["cam_id"]) for item in result["inactive"]}
        save_inactive(inactive_set)
        return result

    @app.get("/api/cameras/inactive")
    async def get_inactive():
        """Return list of inactive (orphaned) camera IDs."""
        pairs = load_inactive()
        return {
            "count": len(pairs),
            "inactive": [{"nvr_id": n, "cam_id": c} for n, c in sorted(pairs)],
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


DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hikvision NVR Recording Monitor</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: Arial, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }
    h1 { margin-bottom: 4px; }
    .muted { color: #94a3b8; }
    .tabs { display: flex; gap: 0; margin: 18px 0; border-bottom: 2px solid #1e293b; }
    .tab { padding: 10px 20px; cursor: pointer; border: 1px solid transparent; border-bottom: none;
            border-radius: 8px 8px 0 0; background: transparent; color: #94a3b8; font-size: 14px; }
    .tab.active { background: #1e293b; color: #e2e8f0; border-color: #334155; border-bottom-color: #1e293b; }
    .tab:hover { color: #e2e8f0; }
    .page { display: none; }
    .page.active { display: block; }
    .toolbar, .filters { display: flex; gap: 12px; align-items: center; margin: 18px 0; flex-wrap: wrap; }
    button, select, input { border: 1px solid #334155; padding: 10px 14px; border-radius: 8px;
                            font-size: 13px; }
    button { background: #2563eb; color: white; cursor: pointer; }
    button:hover { background: #1d4ed8; }
    button.danger { background: #dc2626; }
    button.danger:hover { background: #b91c1c; }
    button.secondary { background: #475569; }
    button.secondary:hover { background: #64748b; }
    select, input { background: #111827; color: #e2e8f0; }
    input { min-width: 280px; }
    table { width: 100%; border-collapse: collapse; background: #111827; border-radius: 12px; overflow: hidden; }
    th, td { padding: 12px; border-bottom: 1px solid #1f2937; text-align: left; vertical-align: top; }
    th { background: #1e293b; color: #cbd5e1; position: sticky; top: 0; }
    .badge { padding: 4px 10px; border-radius: 999px; color: white; font-weight: bold; text-transform: uppercase; font-size: 12px; }
    .ok { background: #16a34a; } .stale { background: #f97316; } .missing { background: #dc2626; } .error { background: #7c2d12; } .unknown { background: #64748b; }
    .card { background: #111827; padding: 16px; border-radius: 12px; margin-bottom: 16px; }
    a { color: #93c5fd; }
    .nvr-link { font-weight: 700; cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }
    .counts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .pill { background: #1e293b; border-radius: 999px; padding: 5px 10px; color: #cbd5e1; font-size: 12px; }
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; align-items: center; justify-content: center; }
    .modal-overlay.open { display: flex; }
    .modal { background: #1e293b; border-radius: 12px; padding: 24px; min-width: 400px; max-width: 500px; }
    .modal h3 { margin-top: 0; }
    .modal label { display: block; margin: 10px 0 4px; color: #94a3b8; font-size: 12px; text-transform: uppercase; }
    .modal input { width: 100%; min-width: 0; }
    .modal .btn-row { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
    td.actions { white-space: nowrap; }
    td.actions button { padding: 6px 10px; font-size: 12px; margin-right: 4px; }
  </style>
</head>
<body>
  <h1>Hikvision NVR Recording Monitor</h1>
  <div class="muted">Per-camera latest recording checker using Hikvision ISAPI.</div>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('cameras')">Cameras</div>
    <div class="tab" onclick="switchTab('nvrs')">NVR Management</div>
  </div>

  <!-- ============ CAMERAS TAB ============ -->
  <div id="page-cameras" class="page active">
    <div class="toolbar">
      <button onclick="pollNow()">Poll now</button>
      <button onclick="reloadConfig()">Rescan DB</button>
      <span id="summary" class="muted">Loading…</span>
    </div>
    <div class="card">
      <div>Overall: <span id="overall" class="badge unknown">unknown</span></div>
      <div class="muted" id="pollInfo"></div>
      <div class="counts" id="counts"></div>
    </div>
    <div class="filters">
      <input id="searchBox" type="search" placeholder="Search NVR, camera, ID, error…" oninput="renderRows()" />
      <select id="statusFilter" onchange="renderRows()">
        <option value="all">All statuses</option>
        <option value="ok">OK</option>
        <option value="stale">Stale</option>
        <option value="missing">Missing</option>
        <option value="error">Error</option>
      </select>
      <select id="nvrFilter" onchange="renderRows()"><option value="all">All NVRs</option></select>
      <button onclick="clearFilters()">Clear filters</button>
      <span id="visibleCount" class="muted"></span>
    </div>
    <table>
      <thead><tr><th>NVR</th><th>Camera</th><th>Status</th><th>Last Recording End</th><th>Age</th><th>Error / Playback</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <!-- ============ NVR MANAGEMENT TAB ============ -->
  <div id="page-nvrs" class="page">
    <div class="toolbar">
      <button onclick="openNvrModal()">+ Add NVR</button>
      <button class="secondary" onclick="reloadConfig()">Rescan DB</button>
      <button onclick="clockCheck()">⏱ Check Clocks</button>
      <button onclick="syncClocks()">🔄 Sync Clocks</button>
      <button onclick="verifyChannels()">✓ Verify Channels</button>
      <span id="nvrSummary" class="muted"></span>
    </div>
    <div id="clockStatus" class="muted" style="margin-bottom:12px"></div>
    <table>
      <thead><tr><th>ID</th><th>Location</th><th>IP</th><th>Username</th><th>Password</th><th>Port</th><th>Clock</th><th>Actions</th></tr></thead>
      <tbody id="nvrRows"></tbody>
    </table>
  </div>

  <!-- ============ NVR MODAL ============ -->
  <div id="nvrModal" class="modal-overlay" onclick="if(event.target===this)closeNvrModal()">
    <div class="modal">
      <h3 id="nvrModalTitle">Add NVR</h3>
      <input type="hidden" id="nvrFormId" />
      <label>Location / Name</label>
      <input id="nvrFormLocation" placeholder="e.g. Base1 NVR1" />
      <label>IP Address</label>
      <input id="nvrFormIp" placeholder="e.g. 172.30.1.253" />
      <label>Username</label>
      <input id="nvrFormUsername" placeholder="e.g. admin" />
      <label>Password</label>
      <input id="nvrFormPassword" type="password" placeholder="Hikvision password" />
      <label>Port</label>
      <input id="nvrFormPort" type="number" value="80" style="min-width:0;width:100px" />
      <div class="btn-row">
        <button class="secondary" onclick="closeNvrModal()">Cancel</button>
        <button onclick="saveNvr()">Save</button>
      </div>
    </div>
  </div>

<script>
let allCameras = [];
let allNvrs = [];
let editingNvrId = null;

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelector(`.tab[onclick*="${name}"]`).classList.add('active');
  document.getElementById(`page-${name}`).classList.add('active');
  if (name === 'nvrs') loadNvrs();
}

function badge(status) { return `<span class="badge ${status}">${status}</span>`; }
function fmt(value) { return value ? new Date(value).toLocaleString() : '-'; }
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

// ── Reload Config ────────────────────────────────────────────
async function reloadConfig() {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = 'Rescanning…'; }
  try {
    const res = await fetch('/api/reload-config', {method:'POST'});
    const data = await res.json();
    alert(`DB rescanned: ${data.nvr_count} NVRs loaded`);
  } catch(e) {
    alert('Reload failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Rescan DB'; }
  }
  await refresh();
  if (document.getElementById('page-nvrs').classList.contains('active')) loadNvrs();
}

// ── Camera Status ────────────────────────────────────────────
function populateNvrFilter(cameras) {
  const current = document.getElementById('nvrFilter').value;
  const nvrs = [...new Set(cameras.map(c => c.nvr_name))].sort((a, b) => a.localeCompare(b));
  document.getElementById('nvrFilter').innerHTML = '<option value="all">All NVRs</option>' +
    nvrs.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
  if (nvrs.includes(current)) document.getElementById('nvrFilter').value = current;
}
function renderCounts(cameras) {
  const counts = cameras.reduce((acc, c) => { acc[c.status] = (acc[c.status] || 0) + 1; return acc; }, {});
  document.getElementById('counts').innerHTML = ['ok','stale','missing','error']
    .map(s => `<span class="pill"><span class="badge ${s}">${s}</span> ${counts[s] || 0}</span>`).join('');
}
function filteredCameras() {
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  const status = document.getElementById('statusFilter').value;
  const nvr = document.getElementById('nvrFilter').value;
  return allCameras.filter(c => {
    if (status !== 'all' && c.status !== status) return false;
    if (nvr !== 'all' && c.nvr_name !== nvr) return false;
    if (!q) return true;
    const haystack = [c.nvr_name, c.nvr_host, c.camera_name, c.camera_id, c.status, c.error, c.last_end_time]
      .map(v => String(v ?? '').toLowerCase()).join(' ');
    return haystack.includes(q);
  });
}
function renderRows() {
  const rows = filteredCameras();
  document.getElementById('visibleCount').textContent = `${rows.length} shown`;
  document.getElementById('rows').innerHTML = rows.map(c => {
    const nvrCell = c.nvr_web_url
      ? `<a class="nvr-link" href="${escapeHtml(c.nvr_web_url)}" title="Open ${escapeHtml(c.nvr_name)} web UI" target="_blank" rel="noopener noreferrer">${escapeHtml(c.nvr_name)}</a><div class="muted">${escapeHtml(c.nvr_host || '')}</div>`
      : `${escapeHtml(c.nvr_name)}`;
    const playback = c.error ? escapeHtml(c.error) : (c.playback_uri ? `<a href="${escapeHtml(c.playback_uri)}" title="Click to open in VLC">\\u25b6 rtsp://${escapeHtml(c.nvr_name.replace(/ /g,""))}@.../</a>` : '-');
    return `
    <tr>
      <td>${nvrCell}</td>
      <td>${escapeHtml(c.camera_name)} <span class="muted">(${escapeHtml(c.camera_id)})</span></td>
      <td>${badge(c.status)}</td>
      <td>${fmt(c.last_end_time)}</td>
      <td>${c.age_minutes == null ? '-' : escapeHtml(c.age_minutes) + ' min'}</td>
      <td>${playback}</td>
    </tr>`;
  }).join('');
}
function clearFilters() {
  document.getElementById('searchBox').value = '';
  document.getElementById('statusFilter').value = 'all';
  document.getElementById('nvrFilter').value = 'all';
  renderRows();
}
async function refresh() {
  const res = await fetch('/api/status');
  const data = await res.json();
  allCameras = data.cameras;
  const overall = document.getElementById('overall');
  overall.className = `badge ${data.overall_status}`;
  overall.textContent = data.overall_status;
  document.getElementById('summary').textContent = `${data.cameras.length} cameras monitored; stale threshold ${data.stale_after_minutes} min`;
  document.getElementById('pollInfo').textContent = `Last poll: ${fmt(data.last_poll_finished_at)}; auto-refresh: ${data.poll_interval_seconds}s`;
  populateNvrFilter(data.cameras);
  renderCounts(data.cameras);
  renderRows();
}
async function pollNow() { await fetch('/api/poll-now', {method:'POST'}); await refresh(); }
refresh(); setInterval(refresh, 30000);

// ── NVR Management ───────────────────────────────────────────
let clockData = {};

async function loadNvrs() {
  const res = await fetch('/api/nvrs');
  allNvrs = await res.json();
  document.getElementById('nvrSummary').textContent = `${allNvrs.length} NVRs configured`;
  renderNvrRows();
}

function renderNvrRows() {
  const haveClock = Object.keys(clockData).length > 0;
  document.getElementById('nvrRows').innerHTML = allNvrs.map(n => {
    const cd = clockData[n.nvr_id];
    let clockCell = '-';
    if (cd) {
      if (cd.status === 'ok') {
        const cls = Math.abs(cd.offset_minutes) < 1 ? 'badge ok' :
                    Math.abs(cd.offset_minutes) < 15 ? 'badge stale' : 'badge missing';
        clockCell = `<span class="${cls}">${escapeHtml(cd.offset_label)}</span>`;
      } else {
        clockCell = `<span class="badge error">${escapeHtml(cd.error || '?')}</span>`;
      }
    }
    return `
    <tr>
      <td>${n.nvr_id}</td>
      <td>${escapeHtml(n.location)}</td>
      <td>${escapeHtml(n.ip)}</td>
      <td>${escapeHtml(n.username)}</td>
      <td>${escapeHtml(n.password)}</td>
      <td>${n.port}</td>
      <td>${clockCell}</td>
      <td class="actions">
        <button onclick="editNvr(${n.nvr_id})">Edit</button>
        <button class="danger" onclick="deleteNvr(${n.nvr_id})">Delete</button>
      </td>
    </tr>`;
  }).join('');
  document.getElementById('clockStatus').textContent = haveClock
    ? `Last clock check: ${Object.keys(clockData).length} NVRs`
    : '';
}

async function clockCheck() {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try {
    const res = await fetch('/api/nvrs/clock-check');
    const data = await res.json();
    clockData = {};
    for (const r of data.results) {
      clockData[r.nvr_id] = r;
    }
    renderNvrRows();
    const msg = `${data.checked} reachable, ${data.unreachable} unreachable`;
    document.getElementById('clockStatus').textContent = msg;
  } catch(e) {
    alert('Clock check failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⏱ Check Clocks'; }
  }
}

async function syncClocks() {
  if (!confirm('Sync clocks on all reachable NVRs? This will set each NVR to current PHT time via ISAPI.')) return;
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
  try {
    const res = await fetch('/api/nvrs/sync-time', {method:'POST'});
    const data = await res.json();
    // Re-check after sync
    await clockCheck();
    const msg = `${data.synced} synced, ${data.failed} failed, ${data.skipped} skipped`;
    document.getElementById('clockStatus').textContent = msg;
    alert(`Sync complete: ${msg}`);
  } catch(e) {
    alert('Sync failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Sync Clocks'; }
  }
}

async function verifyChannels() {
  if (!confirm('Scan all NVRs for their actual channel list? This will detect cameras that were removed from NVRs but still exist in the database.')) return;
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  try {
    const res = await fetch('/api/cameras/verify-channels', {method:'POST'});
    const data = await res.json();
    const msg = `${data.inactive_count} orphan cameras found (in DB but not on NVR). ${data.reachable}/${data.nvr_count} NVRs scanned.`;
    document.getElementById('clockStatus').textContent = msg;
    if (data.inactive_count > 0) {
      alert(`Verify complete: ${data.inactive_count} inactive cameras detected.\n\nThese cameras exist in the database but are no longer configured on their NVRs. They will be excluded from monitoring.`);
    } else {
      alert(`Verify complete: No orphaned cameras found. All DB cameras match NVR channels.`);
    }
  } catch(e) {
    alert('Verify failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '✓ Verify Channels'; }
  }
}

function openNvrModal(nvr) {
  editingNvrId = nvr ? nvr.nvr_id : null;
  document.getElementById('nvrModalTitle').textContent = nvr ? 'Edit NVR' : 'Add NVR';
  document.getElementById('nvrFormId').value = nvr ? nvr.nvr_id : '';
  document.getElementById('nvrFormLocation').value = nvr ? nvr.location : '';
  document.getElementById('nvrFormIp').value = nvr ? nvr.ip : '';
  document.getElementById('nvrFormUsername').value = nvr ? nvr.username : '';
  document.getElementById('nvrFormPassword').value = nvr ? (nvr.password === '••••••' ? '' : nvr.password) : '';
  document.getElementById('nvrFormPort').value = nvr ? nvr.port : 80;
  document.getElementById('nvrModal').classList.add('open');
}

function closeNvrModal() {
  document.getElementById('nvrModal').classList.remove('open');
}

async function saveNvr() {
  const body = {
    location: document.getElementById('nvrFormLocation').value.trim(),
    ip: document.getElementById('nvrFormIp').value.trim(),
    username: document.getElementById('nvrFormUsername').value.trim(),
    password: document.getElementById('nvrFormPassword').value,
    port: parseInt(document.getElementById('nvrFormPort').value) || 80,
  };
  if (!body.location || !body.ip) { alert('Location and IP are required'); return; }
  try {
    if (editingNvrId) {
      await fetch(`/api/nvrs/${editingNvrId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
    } else {
      await fetch('/api/nvrs', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
    }
    closeNvrModal();
    await loadNvrs();
  } catch(e) {
    alert('Save failed: ' + e.message);
  }
}

async function editNvr(nvrId) {
  const nvr = allNvrs.find(n => n.nvr_id === nvrId);
  if (nvr) openNvrModal(nvr);
}

async function deleteNvr(nvrId) {
  const nvr = allNvrs.find(n => n.nvr_id === nvrId);
  if (!confirm(`Delete NVR "${nvr?.location}" (ID ${nvrId})?`)) return;
  try {
    await fetch(`/api/nvrs/${nvrId}`, {method: 'DELETE'});
    await loadNvrs();
  } catch(e) {
    alert('Delete failed: ' + e.message);
  }
}
</script>
</body>
</html>
"""

app = build_app(os.getenv("HIKVISION_MONITOR_CONFIG"))
