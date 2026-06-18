"""Channel verification — compare NVR channel lists vs DB cameras and persist channel enabled state."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import httpx

INACTIVE_FILE = "/app/app/inactive_cameras.json"
SETTINGS_TABLE = "[NVRTest].[dbo].[NVRChannelSettings]"


def _connect():
    import pyodbc
    return pyodbc.connect(os.environ["NVR_SQLSERVER_CONNECTION_STRING"])


def ensure_channel_settings_table(conn=None):
    """Create SQL-backed channel enabled/disabled settings table if missing."""
    own_conn = conn is None
    conn = conn or _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
IF OBJECT_ID(N'[NVRTest].[dbo].[NVRChannelSettings]', N'U') IS NULL
BEGIN
    CREATE TABLE [NVRTest].[dbo].[NVRChannelSettings] (
        [nvr_id] INT NOT NULL,
        [cam_id] INT NOT NULL,
        [enabled] BIT NOT NULL CONSTRAINT DF_NVRChannelSettings_enabled DEFAULT (1),
        [updated_at] DATETIME2 NOT NULL CONSTRAINT DF_NVRChannelSettings_updated_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_NVRChannelSettings PRIMARY KEY ([nvr_id], [cam_id])
    );
END
"""
    )
    conn.commit()
    if own_conn:
        conn.close()


def ensure_channel_setting(conn, nvr_id: int, cam_id: int, *, enabled: bool = True):
    """Ensure a channel has an explicit row in SQL settings table."""
    ensure_channel_settings_table(conn)
    cursor = conn.cursor()
    cursor.execute(
        f"""
IF NOT EXISTS (SELECT 1 FROM {SETTINGS_TABLE} WHERE [nvr_id] = ? AND [cam_id] = ?)
BEGIN
    INSERT INTO {SETTINGS_TABLE} ([nvr_id], [cam_id], [enabled]) VALUES (?, ?, ?)
END
""",
        nvr_id, cam_id, nvr_id, cam_id, 1 if enabled else 0,
    )
    conn.commit()


def set_channel_enabled(conn, nvr_id: int, cam_id: int, enabled: bool):
    """Upsert explicit enabled/disabled state for a channel."""
    ensure_channel_settings_table(conn)
    cursor = conn.cursor()
    cursor.execute(
        f"""
MERGE {SETTINGS_TABLE} AS target
USING (SELECT ? AS nvr_id, ? AS cam_id) AS src
ON target.[nvr_id] = src.nvr_id AND target.[cam_id] = src.cam_id
WHEN MATCHED THEN
    UPDATE SET [enabled] = ?, [updated_at] = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT ([nvr_id], [cam_id], [enabled]) VALUES (?, ?, ?);
""",
        nvr_id, cam_id, 1 if enabled else 0, nvr_id, cam_id, 1 if enabled else 0,
    )
    conn.commit()


def load_channel_settings(conn=None) -> dict[tuple[int, int], bool]:
    """Load explicit channel enabled/disabled settings from SQL."""
    try:
        own_conn = conn is None
        conn = conn or _connect()
        ensure_channel_settings_table(conn)
        cursor = conn.cursor()
        rows = cursor.execute(f"SELECT [nvr_id], [cam_id], [enabled] FROM {SETTINGS_TABLE}").fetchall()
        result = {(int(r[0]), int(r[1])): bool(r[2]) for r in rows}
        if own_conn:
            conn.close()
        return result
    except Exception:
        # Fallback for local tests/non-SQL mode.
        return {(n, c): False for n, c in _load_inactive_file()}


def _load_inactive_file() -> set[tuple[int, int]]:
    try:
        data = json.loads(Path(INACTIVE_FILE).read_text())
        return {(item["nvr_id"], item["cam_id"]) for item in data}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def load_inactive() -> set[tuple[int, int]]:
    """Load disabled (nvr_id, cam_id) pairs from SQL settings table."""
    settings = load_channel_settings()
    return {pair for pair, enabled in settings.items() if not enabled}


def save_inactive(pairs: set[tuple[int, int]]):
    """Persist disabled pairs to SQL settings table, replacing current disabled set.

    Existing enabled rows stay enabled; any previously disabled row not in `pairs` is marked enabled.
    """
    try:
        conn = _connect()
        ensure_channel_settings_table(conn)
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {SETTINGS_TABLE} SET [enabled] = 1, [updated_at] = SYSUTCDATETIME()")
        for nvr_id, cam_id in sorted(pairs):
            set_channel_enabled(conn, nvr_id, cam_id, False)
        conn.commit()
        conn.close()
        print(f"Saved {len(pairs)} disabled cameras to SQL table {SETTINGS_TABLE}")
    except Exception:
        data = [{"nvr_id": n, "cam_id": c} for n, c in sorted(pairs)]
        Path(INACTIVE_FILE).write_text(json.dumps(data, indent=2))
        print(f"Saved {len(pairs)} inactive cameras to fallback file {INACTIVE_FILE}")


async def get_nvr_channel_ids(
    host: str, username: str, password: str, timeout: float = 10
) -> set[int] | None:
    """Get the set of active channel IDs from an NVR via ISAPI.
    Returns None if the NVR is unreachable."""
    url = f"http://{host}:80/ISAPI/ContentMgmt/InputProxy/channels"
    auth = httpx.DigestAuth(username, password)
    try:
        async with httpx.AsyncClient(auth=auth, timeout=timeout, verify=False) as client:
            r = await client.get(url)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            for node in root.iter():
                if "}" in node.tag:
                    node.tag = node.tag.rsplit("}", 1)[1]
            channels = set()
            for ch in root.findall("InputProxyChannel"):
                id_el = ch.find("id")
                if id_el is not None:
                    channels.add(int(id_el.text))
            return channels
    except Exception:
        return None


async def verify_all_channels(nvrs: list[dict]) -> dict:
    """Verify all NVRs' channels against DB cameras.
    Returns dict with inactive pairs, summary stats."""
    conn = _connect()
    ensure_channel_settings_table(conn)
    cursor = conn.cursor()

    tasks = []
    for n in nvrs:
        if not n.get("ip"):
            continue
        tasks.append(get_nvr_channel_ids(n["ip"], n["username"], n["password"]))

    channel_lists = await asyncio.gather(*tasks)
    nvr_ids = [n["nvr_id"] for n in nvrs if n.get("ip")]

    inactive: set[tuple[int, int]] = set()
    discovered: set[tuple[int, int]] = set()
    reachable = 0
    unreachable = 0

    for nvr_id, nvr_channels in zip(nvr_ids, channel_lists):
        if nvr_channels is None:
            unreachable += 1
            continue
        reachable += 1

        rows = cursor.execute(
            "SELECT cam_id FROM [NVRTest].[dbo].[cameraresults] "
            "WHERE nvr_id = ? AND cam_id IS NOT NULL AND cam_id != 0 "
            "GROUP BY nvr_id, cam_id",
            nvr_id,
        ).fetchall()
        db_cam_ids = {int(r[0]) for r in rows}

        for cid in db_cam_ids:
            ensure_channel_setting(conn, nvr_id, cid, enabled=True)

        orphan_ids = db_cam_ids - nvr_channels
        for cid in orphan_ids:
            inactive.add((nvr_id, cid))
            set_channel_enabled(conn, nvr_id, cid, False)

        new_ids = nvr_channels - db_cam_ids
        if new_ids:
            for cam_id in sorted(new_ids):
                cam_name = f"IPCamera {cam_id}"
                cursor.execute(
                    "INSERT INTO [NVRTest].[dbo].[cameraresults] "
                    "(nvr_id, camera, cam_id) VALUES (?, ?, ?)",
                    nvr_id, cam_name, cam_id,
                )
                ensure_channel_setting(conn, nvr_id, cam_id, enabled=True)
                discovered.add((nvr_id, cam_id))
            conn.commit()

    return {
        "nvr_count": len(nvr_ids),
        "reachable": reachable,
        "unreachable": unreachable,
        "total_db_cameras": _count_db_cameras(),
        "total_nvr_channels": _sum_channel_lists(channel_lists),
        "inactive_count": len(inactive),
        "discovered_count": len(discovered),
        "inactive": [{"nvr_id": n, "cam_id": c} for n, c in sorted(inactive)],
        "discovered": [{"nvr_id": n, "cam_id": c} for n, c in sorted(discovered)],
    }


def _count_db_cameras() -> int:
    conn = _connect()
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT COUNT(*) FROM (SELECT nvr_id, cam_id FROM [NVRTest].[dbo].[cameraresults] "
        "WHERE cam_id IS NOT NULL AND cam_id != 0 GROUP BY nvr_id, cam_id) t"
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def _sum_channel_lists(lists: list[set[int] | None]) -> int:
    return sum(len(s) for s in lists if s is not None)
