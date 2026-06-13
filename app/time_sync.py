"""NVR clock check and sync via Hikvision ISAPI System/time."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import httpx

PHT = timezone(timedelta(hours=8))
PHT_STR = "+08:00"


def format_hik_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + PHT_STR


async def get_nvr_time(
    host: str, username: str, password: str, timeout: float = 10
) -> dict:
    """Read NVR's current time via ISAPI GET /System/time. Returns dict with keys:
    status, nvr_time, time_mode, offset_minutes, error (if any)."""
    url = f"http://{host}:80/ISAPI/System/time"
    auth = httpx.DigestAuth(username, password)
    try:
        async with httpx.AsyncClient(auth=auth, timeout=timeout, verify=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            xml = resp.text
    except Exception as e:
        return {"status": "unreachable", "error": str(e)[:60]}

    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    for node in root.iter():
        if "}" in node.tag:
            node.tag = node.tag.rsplit("}", 1)[1]

    result = {}
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        result[tag] = (child.text or "").strip()

    time_mode = result.get("timeMode", "?")
    local_time = result.get("localTime", "")

    if not local_time:
        return {"status": "error", "error": "no localTime in response",
                "time_mode": time_mode}

    try:
        nv = local_time.strip()
        if nv.endswith("Z"):
            nv = nv[:-1] + "+00:00"
        nvr_dt = datetime.fromisoformat(nv)
        now_utc = datetime.now(timezone.utc)
        diff_min = round((nvr_dt - now_utc).total_seconds() / 60, 1)
    except Exception as e:
        return {"status": "error", "error": f"parse error: {e}",
                "time_mode": time_mode, "nvr_time": local_time}

    if abs(diff_min) < 0.5:
        offset_label = "synced"
    elif diff_min > 0:
        offset_label = f"+{diff_min:.0f}m"
    else:
        offset_label = f"{diff_min:.0f}m"

    return {
        "status": "ok",
        "nvr_time": local_time,
        "time_mode": time_mode,
        "offset_minutes": diff_min,
        "offset_label": offset_label,
    }


async def sync_nvr_time(
    host: str, username: str, password: str, nvr_info: dict | None = None,
    timeout: float = 10
) -> dict:
    """Sync NVR clock to current PHT time via ISAPI PUT /System/time.
    Returns dict with status and optional error."""
    url = f"http://{host}:80/ISAPI/System/time"
    auth = httpx.DigestAuth(username, password)
    target_local = format_hik_time(datetime.now(PHT))
    restore_ntp = bool(nvr_info and nvr_info.get("time_mode") == "NTP")

    xml_set = f"""<?xml version="1.0" encoding="UTF-8" ?>
<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>{target_local}</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""

    xml_restore = f"""<?xml version="1.0" encoding="UTF-8" ?>
<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>NTP</timeMode>
<localTime>{target_local}</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""

    try:
        async with httpx.AsyncClient(auth=auth, timeout=timeout, verify=False) as client:
            # Step 1: set manual time
            r1 = await client.put(url, content=xml_set.encode("utf-8"),
                                  headers={"Content-Type": "application/xml"})
            r1.raise_for_status()
            # Step 2: restore NTP mode if it was originally NTP
            if restore_ntp:
                r2 = await client.put(url, content=xml_restore.encode("utf-8"),
                                      headers={"Content-Type": "application/xml"})
                r2.raise_for_status()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:80]}


async def clock_check_all(nvrs: list[dict]) -> list[dict]:
    """Check clock offset for all reachable NVRs."""
    tasks = []
    for n in nvrs:
        if not n.get("ip"):
            tasks.append(_skip_result(n, "no IP"))
            continue
        tasks.append(_check_one(n))
    results = await asyncio.gather(*tasks)
    return sorted(results, key=lambda r: (abs(r.get("offset_minutes", 9999) or 9999)), reverse=True)


async def _check_one(n: dict) -> dict:
    info = await get_nvr_time(n["ip"], n["username"], n["password"])
    return {
        "nvr_id": n["nvr_id"],
        "location": n["location"],
        "ip": n["ip"],
        **info,
    }


def _skip_result(n: dict, reason: str) -> dict:
    return {
        "nvr_id": n["nvr_id"],
        "location": n["location"],
        "ip": n.get("ip", ""),
        "status": "skipped",
        "error": reason,
    }


async def sync_all(nvrs: list[dict]) -> list[dict]:
    """Sync clocks of all reachable NVRs. First reads current time to detect mode."""
    check_results = await clock_check_all(nvrs)
    tasks = []
    for r in check_results:
        if r["status"] != "ok":
            tasks.append(_skip_result_nvr(r, f"unreachable ({r.get('error','?')})"))
            continue
        tasks.append(_sync_one(r))
    sync_results = await asyncio.gather(*tasks)
    return sync_results


async def _sync_one(r: dict) -> dict:
    result = await sync_nvr_time(r["ip"], r.get("username", "admin"),
                                  r.get("password", ""), r)
    return {
        "nvr_id": r["nvr_id"],
        "location": r["location"],
        "ip": r["ip"],
        **result,
    }


async def _skip_result_nvr(r: dict, reason: str) -> dict:
    return {
        "nvr_id": r["nvr_id"],
        "location": r["location"],
        "ip": r.get("ip", ""),
        "status": "skipped",
        "error": reason,
    }
