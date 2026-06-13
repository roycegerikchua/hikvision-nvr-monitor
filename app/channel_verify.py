"""Channel verification — compare NVR channel lists vs DB cameras."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import httpx

INACTIVE_FILE = "/app/app/inactive_cameras.json"


def load_inactive() -> set[tuple[int, int]]:
    """Load cached inactive (nvr_id, cam_id) pairs from JSON file."""
    try:
        data = json.loads(Path(INACTIVE_FILE).read_text())
        return {(item["nvr_id"], item["cam_id"]) for item in data}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_inactive(pairs: set[tuple[int, int]]):
    """Save inactive (nvr_id, cam_id) pairs to JSON file."""
    data = [{"nvr_id": n, "cam_id": c} for n, c in sorted(pairs)]
    Path(INACTIVE_FILE).write_text(json.dumps(data, indent=2))
    print(f"Saved {len(pairs)} inactive cameras to {INACTIVE_FILE}")


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
    import pyodbc

    # Get all active DB cameras (de-duped by latest line_id)
    conn = pyodbc.connect(os.environ["NVR_SQLSERVER_CONNECTION_STRING"])
    cursor = conn.cursor()

    # Check what's in the DB
    tasks = []
    nvr_map = {}
    for n in nvrs:
        if not n.get("ip"):
            continue
        tasks.append(get_nvr_channel_ids(n["ip"], n["username"], n["password"]))
        nvr_map[n["nvr_id"]] = n

    channel_lists = await asyncio.gather(*tasks)

    # Collect NVR IDs in order of tasks
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

        # Get DB cameras for this NVR
        rows = cursor.execute(
            "SELECT cam_id FROM [NVRTest].[dbo].[cameraresults] "
            "WHERE nvr_id = ? AND cam_id IS NOT NULL AND cam_id != 0 "
            "GROUP BY nvr_id, cam_id",
            nvr_id
        ).fetchall()
        db_cam_ids = {r[0] for r in rows}

        # Find orphans: in DB but not on NVR
        orphan_ids = db_cam_ids - nvr_channels
        for cid in orphan_ids:
            inactive.add((nvr_id, cid))

        # Find new cameras: on NVR but not in DB → auto-discover
        new_ids = nvr_channels - db_cam_ids
        if new_ids:
            for cam_id in sorted(new_ids):
                cam_name = f"IPCamera {cam_id}"
                cursor.execute(
                    "INSERT INTO [NVRTest].[dbo].[cameraresults] "
                    "(nvr_id, camera, cam_id) VALUES (?, ?, ?)",
                    nvr_id, cam_name, cam_id,
                )
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
    import pyodbc
    conn = pyodbc.connect(os.environ["NVR_SQLSERVER_CONNECTION_STRING"])
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT COUNT(*) FROM (SELECT nvr_id, cam_id FROM [NVRTest].[dbo].[cameraresults] "
        "WHERE cam_id IS NOT NULL AND cam_id != 0 GROUP BY nvr_id, cam_id) t"
    ).fetchone()
    return row[0] if row else 0


def _sum_channel_lists(lists: list[set[int] | None]) -> int:
    return sum(len(s) for s in lists if s is not None)
