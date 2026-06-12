"""Check and optionally sync NVR clocks via Hikvision ISAPI System/time."""
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

import httpx

# PHT timezone = UTC+8
PHT = timezone(timedelta(hours=8))

NVR_SQLSERVER_CONNECTION_STRING = os.environ.get("NVR_SQLSERVER_CONNECTION_STRING", "")
if not NVR_SQLSERVER_CONNECTION_STRING:
    print("ERROR: NVR_SQLSERVER_CONNECTION_STRING not set")
    raise SystemExit(1)


def get_nvrs_from_db():
    """Read NVRs from database - returns list of dicts."""
    import pyodbc
    conn = pyodbc.connect(NVR_SQLSERVER_CONNECTION_STRING)
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT nvr_id, location, ip, username, password, port "
        "FROM [NVRTest].[dbo].[NVR] WHERE ip IS NOT NULL AND ip != '' "
        "ORDER BY nvr_id"
    ).fetchall()
    return [
        {"nvr_id": r[0], "location": r[1], "ip": r[2],
         "username": r[3], "password": r[4], "port": r[5]}
        for r in rows
    ]


async def get_nvr_time(nvr, timeout=10):
    """Get current time from NVR via ISAPI. Returns dict or None."""
    host = nvr["ip"]
    port = 80  # Hikvision HTTP API always on port 80
    url = f"http://{host}:{port}/ISAPI/System/time"
    auth = httpx.DigestAuth(nvr["username"], nvr["password"])
    try:
        async with httpx.AsyncClient(auth=auth, timeout=timeout, verify=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return {"status": "ok", "xml": resp.text, "nvr": nvr}
    except Exception as e:
        return {"status": "error", "error": str(e), "nvr": nvr}


def parse_nvr_time(xml_text):
    """Extract timeMode, localTime, timeZone from Hikvision time XML."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    # Strip namespace if present
    for node in root.iter():
        if "}" in node.tag:
            node.tag = node.tag.rsplit("}", 1)[1]
    result = {}
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        result[tag] = (child.text or "").strip()
    return result


def parse_hik_time_to_dt(time_str):
    """Parse Hikvision DateTimeISO8601 format to datetime."""
    # Format: 2026-06-12T20:55:00+08:00 or 2026-06-12T12:55:00Z
    normalized = time_str.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


def format_hik_time(dt):
    """Format datetime to Hikvision format with timezone offset."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


async def sync_nvr_time(nvr, target_time, nvr_info, timeout=10):
    """Set NVR time via ISAPI PUT /ISAPI/System/time.
    
    Strategy: GET current XML, modify time (+ switch to manual if needed),
    PUT back, then restore NTP mode if it was originally NTP.
    """
    host = nvr["ip"]
    port = 80
    url = f"http://{host}:{port}/ISAPI/System/time"
    auth = httpx.DigestAuth(nvr["username"], nvr["password"])
    target_local = format_hik_time(target_time)

    async with httpx.AsyncClient(auth=auth, timeout=timeout, verify=False) as client:
        restore_ntp = False
        if nvr_info and nvr_info.get("timeMode") == "NTP":
            restore_ntp = True

        try:
            if restore_ntp:
                # Step 1: switch to manual with correct time
                xml_manual = f"""<?xml version="1.0" encoding="UTF-8" ?>
<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>{target_local}</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""
                r1 = await client.put(url, content=xml_manual.encode("utf-8"),
                                      headers={"Content-Type": "application/xml"})
                r1.raise_for_status()

                # Step 2: switch back to NTP
                xml_ntp = f"""<?xml version="1.0" encoding="UTF-8" ?>
<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>NTP</timeMode>
<localTime>{target_local}</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""
                r2 = await client.put(url, content=xml_ntp.encode("utf-8"),
                                      headers={"Content-Type": "application/xml"})
                r2.raise_for_status()
            else:
                # Manual mode: just set the time
                xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>{target_local}</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""
                r = await client.put(url, content=xml.encode("utf-8"),
                                     headers={"Content-Type": "application/xml"})
                r.raise_for_status()
            return {"status": "ok", "nvr": nvr}
        except Exception as e:
            return {"status": "error", "error": str(e), "nvr": nvr}


async def main():
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"

    nvrs = get_nvrs_from_db()
    print(f"Found {len(nvrs)} NVRs in database")

    if mode == "check":
        print(f"\n{'NVR ID':>7s}  {'Location':25s}  {'IP':16s}  {'NVR Time':22s}  {'Offset':>8s}  {'Time Mode':12s}")
        print("-" * 100)

        tasks = [get_nvr_time(n) for n in nvrs]
        results = await asyncio.gather(*tasks)

        for r in results:
            n = r["nvr"]
            if r["status"] == "error":
                print(f"  {n['nvr_id']:>5d}  {n['location']:25s}  {n['ip']:16s}  {'ERROR':22s}  {'':>8s}  {r['error'][:60]}")
                continue

            parsed = parse_nvr_time(r["xml"])
            local_time = parsed.get("localTime", "")
            time_mode = parsed.get("timeMode", "?")
            offset = ""

            if local_time:
                try:
                    nvr_dt = parse_hik_time_to_dt(local_time)
                    now_utc = datetime.now(timezone.utc)
                    now_pht = now_utc.astimezone(PHT)
                    diff = (nvr_dt - now_utc).total_seconds() / 60
                    if abs(diff) < 1:
                        offset = "OK"
                    elif diff > 0:
                        offset = f"+{diff:.0f} min"
                    else:
                        offset = f"{diff:.0f} min"
                except Exception as e:
                    offset = f"parse err: {e}"
            else:
                local_time = "NOT SET"

            print(f"  {n['nvr_id']:>5d}  {n['location']:25s}  {n['ip']:16s}  {local_time:22s}  {offset:>8s}  {time_mode:12s}")

    elif mode == "sync":
        print("SYNC MODE - This will SET the time on all reachable NVRs!")
        print(f"{'NVR ID':>7s}  {'Location':25s}  {'IP':16s}  {'Old Time':22s}  {'Result':20s}")
        print("-" * 95)

        now_utc = datetime.now(timezone.utc)
        now_pht = now_utc.astimezone(PHT)
        target_time = now_pht  # Set to PHT

        # First read all current times
        check_tasks = [get_nvr_time(n) for n in nvrs]
        check_results = await asyncio.gather(*check_tasks)

        sync_tasks = []
        for r in check_results:
            n = r["nvr"]
            if r["status"] == "error":
                print(f"  {n['nvr_id']:>5d}  {n['location']:25s}  {n['ip']:16s}  {'UNREACHABLE':22s}  {'SKIPPED':20s}")
                continue

            parsed = parse_nvr_time(r["xml"])
            old_time = parsed.get("localTime", "?")
            sync_tasks.append(sync_nvr_time(n, target_time, parsed))

        print(f"\nSyncing {len(sync_tasks)} NVRs to {format_hik_time(target_time)} (PHT)...")
        sync_results = await asyncio.gather(*sync_tasks)

        for r in sync_results:
            n = r["nvr"]
            if r["status"] == "ok":
                print(f"  {n['nvr_id']:>5d}  {n['location']:25s}  {n['ip']:16s}  {'-> synced':22s}  {'OK':20s}")
            else:
                print(f"  {n['nvr_id']:>5d}  {n['location']:25s}  {n['ip']:16s}  {'-> SYNC FAILED':22s}  {r['error'][:20]:20s}")

        # Verify: wait and re-check
        print("\nWaiting 3 seconds then re-checking...")
        await asyncio.sleep(3)
        recheck_tasks = [get_nvr_time(r["nvr"]) for r in sync_results if r["status"] == "ok"]
        recheck_results = await asyncio.gather(*recheck_tasks)
        for r in recheck_results:
            n = r["nvr"]
            if r["status"] == "ok":
                parsed = parse_nvr_time(r["xml"])
                new_time = parsed.get("localTime", "?")
                new_dt = parse_hik_time_to_dt(new_time)
                diff = (new_dt - datetime.now(timezone.utc)).total_seconds()
                print(f"  {n['nvr_id']:>5d}  {n['location']:25s}  now offset: {diff:.0f}s")

    else:
        print(f"Usage: {sys.argv[0]} [check|sync]")
        print("  check  - read and display NVR clock offsets (default)")
        print("  sync   - sync all reachable NVR clocks to PHT")


if __name__ == "__main__":
    asyncio.run(main())
