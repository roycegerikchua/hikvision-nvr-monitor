from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .channel_verify import load_inactive
from .config import AppConfig, CameraConfig, NvrConfig
from .hikvision import CameraRecordingStatus

DEFAULT_QUERY = """\
WITH latest_cam AS (
    SELECT
        c.[nvr_id],
        c.[camera],
        c.[cam_id],
        MAX(c.[line_id]) AS [line_id]
    FROM [NVRTest].[dbo].[cameraresults] c
    WHERE c.[cam_id] IS NOT NULL AND c.[cam_id] != 0
    GROUP BY c.[nvr_id], c.[camera], c.[cam_id]
)
SELECT
    n.[nvr_id],
    n.[location],
    n.[ip],
    n.[username],
    n.[password],
    n.[port],
    l.[line_id],
    l.[camera],
    l.[cam_id]
FROM [NVRTest].[dbo].[NVR] n
JOIN latest_cam l ON l.[nvr_id] = n.[nvr_id]
WHERE n.[ip] IS NOT NULL AND n.[ip] != ''
ORDER BY n.[nvr_id], l.[line_id]
"""

UPDATE_CAMERA_SQL = """\
UPDATE [NVRTest].[dbo].[cameraresults]
SET
    [lastrecording] = ?,
    [online] = ?,
    [lastupdated] = ?
WHERE [line_id] = ?
"""


def _get(row, name: str):
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def rows_to_config(
    rows: Iterable,
    *,
    poll_interval_seconds: int = 300,
    lookback_hours: int = 24,
    stale_after_minutes: int = 30,
) -> AppConfig:
    grouped: dict[int, dict] = {}
    for row in rows:
        nvr_id = int(_get(row, "nvr_id"))
        if nvr_id not in grouped:
            grouped[nvr_id] = {
                "nvr_id": nvr_id,
                "name": str(_get(row, "location")),
                "host": str(_get(row, "ip")),
                "username": str(_get(row, "username")),
                "password": str(_get(row, "password")),
                "port": 80,
                "https": False,
                "cameras": [],
            }
        grouped[nvr_id]["cameras"].append(
            CameraConfig(
                id=str(int(_get(row, "cam_id")) * 100 + 1),
                name=str(_get(row, "camera")),
                line_id=int(_get(row, "line_id")),
            )
        )

    nvrs = [NvrConfig(**item) for item in grouped.values()]
    return AppConfig(
        poll_interval_seconds=poll_interval_seconds,
        lookback_hours=lookback_hours,
        stale_after_minutes=stale_after_minutes,
        nvrs=nvrs,
    )


class SqlServerRepository:
    def __init__(
        self,
        connection,
        *,
        poll_interval_seconds: int = 300,
        lookback_hours: int = 24,
        stale_after_minutes: int = 30,
    ) -> None:
        self.connection = connection
        self.poll_interval_seconds = poll_interval_seconds
        self.lookback_hours = lookback_hours
        self.stale_after_minutes = stale_after_minutes
        self._show_passwords = False

    def load_config(self) -> AppConfig:
        cursor = self.connection.cursor()
        rows = cursor.execute(DEFAULT_QUERY).fetchall()
        # Filter out cameras marked as inactive (removed from NVR or manually disabled)
        inactive = load_inactive()
        if inactive:
            filtered = []
            for row in rows:
                rid = int(_get(row, "nvr_id"))
                # cam_id is the 9th column (index 8) in the SELECT
                cid = int(_get(row, "cam_id"))
                if (rid, cid) not in inactive:
                    filtered.append(row)
            rows = filtered
        return rows_to_config(
            rows,
            poll_interval_seconds=self.poll_interval_seconds,
            lookback_hours=self.lookback_hours,
            stale_after_minutes=self.stale_after_minutes,
        )

    def update_camera_result(self, status: CameraRecordingStatus, *, now: datetime | None = None) -> None:
        if status.line_id is None:
            return
        current = now or datetime.now(timezone.utc)
        online = "1" if status.status != "error" else "0"
        lastrec = None
        if status.last_end_time is not None:
            lastrec = status.last_end_time.astimezone(timezone.utc).strftime("%b %#d %Y %I:%M%p")
        cursor = self.connection.cursor()
        cursor.execute(
            UPDATE_CAMERA_SQL,
            lastrec,
            online,
            current,
            status.line_id,
        )
        self.connection.commit()

    def update_many(self, statuses: Sequence[CameraRecordingStatus], *, now: datetime | None = None) -> None:
        for status in statuses:
            self.update_camera_result(status, now=now)

    # ── NVR CRUD ──────────────────────────────────────────────
    def list_nvrs(self):
        cursor = self.connection.cursor()
        rows = cursor.execute(
            "SELECT nvr_id, location, ip, username, password, port "
            "FROM [NVRTest].[dbo].[NVR] ORDER BY nvr_id"
        ).fetchall()
        return [
            {
                "nvr_id": r[0],
                "location": r[1],
                "ip": r[2],
                "username": r[3],
                "password": r[4] if self._show_passwords else "••••••",
                "port": r[5],
            }
            for r in rows
        ]

    def get_nvr(self, nvr_id: int):
        cursor = self.connection.cursor()
        row = cursor.execute(
            "SELECT nvr_id, location, ip, username, password, port "
            "FROM [NVRTest].[dbo].[NVR] WHERE nvr_id = ?", nvr_id
        ).fetchone()
        if not row:
            return None
        return {"nvr_id": row[0], "location": row[1], "ip": row[2],
                "username": row[3], "password": row[4], "port": row[5]}

    def add_nvr(self, location: str, ip: str, username: str, password: str, port: int = 80):
        cursor = self.connection.cursor()
        cursor.execute(
            "INSERT INTO [NVRTest].[dbo].[NVR] (location, ip, username, password, port) "
            "OUTPUT INSERTED.nvr_id VALUES (?, ?, ?, ?, ?)",
            location, ip, username, password, port,
        )
        self.connection.commit()
        new_id = cursor.fetchone()[0]
        return new_id

    def update_nvr(self, nvr_id: int, location: str, ip: str, username: str, password: str, port: int = 80):
        cursor = self.connection.cursor()
        cursor.execute(
            "UPDATE [NVRTest].[dbo].[NVR] SET location=?, ip=?, username=?, password=?, port=? "
            "WHERE nvr_id=?",
            location, ip, username, password, port, nvr_id,
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def delete_nvr(self, nvr_id: int):
        cursor = self.connection.cursor()
        cursor.execute("DELETE FROM [NVRTest].[dbo].[NVR] WHERE nvr_id=?", nvr_id)
        self.connection.commit()
        return cursor.rowcount > 0


def connect_sql_server_from_env():
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("pyodbc is required for SQL Server mode. Install requirements.txt and Microsoft ODBC Driver 17/18.") from exc

    connection_string = os.getenv("NVR_SQLSERVER_CONNECTION_STRING")
    if not connection_string:
        server = os.getenv("NVR_SQLSERVER_HOST", "localhost")
        database = os.getenv("NVR_SQLSERVER_DATABASE", "NVRTest")
        username = os.getenv("NVR_SQLSERVER_USERNAME")
        password = os.getenv("NVR_SQLSERVER_PASSWORD")
        driver = os.getenv("NVR_SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server")
        trust_cert = os.getenv("NVR_SQLSERVER_TRUST_CERT", "yes")
        if username and password:
            connection_string = (
                f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password};"
                f"TrustServerCertificate={trust_cert};"
            )
        else:
            connection_string = (
                f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
                f"TrustServerCertificate={trust_cert};"
            )
    return pyodbc.connect(connection_string)


def load_config_from_sql_env() -> tuple[AppConfig, SqlServerRepository]:
    conn = connect_sql_server_from_env()
    repo = SqlServerRepository(
        conn,
        poll_interval_seconds=int(os.getenv("NVR_POLL_INTERVAL_SECONDS", "300")),
        lookback_hours=int(os.getenv("NVR_LOOKBACK_HOURS", "24")),
        stale_after_minutes=int(os.getenv("NVR_STALE_AFTER_MINUTES", "30")),
    )
    return repo.load_config(), repo
