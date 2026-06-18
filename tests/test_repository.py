from datetime import datetime, timezone

from app.hikvision import CameraRecordingStatus
from app.repository import SqlServerRepository, rows_to_config


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, *params):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows=None):
        self.cursor_obj = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


class Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_rows_to_config_uses_nvr_table_and_cameraresults_table():
    rows = [
        Row(nvr_id=1, location="Warehouse", ip="192.168.1.10", username="admin", password="pw", port=80, line_id=10, camera="Front", cam_id=1),
        Row(nvr_id=1, location="Warehouse", ip="192.168.1.10", username="admin", password="pw", port=80, line_id=11, camera="Back", cam_id=2),
        Row(nvr_id=2, location="Office", ip="192.168.1.11", username="root", password="pw2", port=8080, line_id=20, camera="Cashier", cam_id=1),
    ]

    config = rows_to_config(rows, poll_interval_seconds=60, lookback_hours=12, stale_after_minutes=45)

    assert config.poll_interval_seconds == 60
    assert config.lookback_hours == 12
    assert config.stale_after_minutes == 45
    assert len(config.nvrs) == 2
    assert config.nvrs[0].nvr_id == 1
    assert config.nvrs[0].name == "Warehouse"
    assert config.nvrs[0].host == "192.168.1.10"
    assert config.nvrs[0].cameras[0].line_id == 10
    assert config.nvrs[0].cameras[0].id == "101"
    assert config.nvrs[0].cameras[1].name == "Back"
    assert config.nvrs[1].port == 80


def test_repository_load_config_executes_expected_join():
    conn = FakeConnection(rows=[Row(nvr_id=1, location="Main", ip="1.1.1.1", username="u", password="p", port=80, line_id=5, camera="Cam", cam_id=101)])
    repo = SqlServerRepository(conn, poll_interval_seconds=30, lookback_hours=6, stale_after_minutes=20)

    config = repo.load_config()

    sql = conn.cursor_obj.executed[0][0]
    assert "FROM [NVRTest].[dbo].[NVR] n" in sql
    assert "JOIN latest_cam l" in sql
    assert config.nvrs[0].cameras[0].line_id == 5


def test_repository_update_camera_result_writes_lastrecording_online_and_lastupdated():
    conn = FakeConnection()
    repo = SqlServerRepository(conn)
    status = CameraRecordingStatus(
        nvr_name="Main",
        nvr_id=1,
        camera_id="101",
        line_id=55,
        camera_name="Front",
        status="ok",
        last_start_time=None,
        last_end_time=datetime(2026, 6, 11, 12, 5, tzinfo=timezone.utc),
        age_minutes=1,
        playback_uri=None,
        error=None,
    )

    repo.update_camera_result(status, now=datetime(2026, 6, 11, 12, 6, tzinfo=timezone.utc))

    sql, params = conn.cursor_obj.executed[0]
    assert "UPDATE [NVRTest].[dbo].[cameraresults]" in sql
    assert "[lastrecording] = ?" in sql
    assert "[online] = ?" in sql
    # lastrecording should be formatted as text, e.g. "Jun 11 2026 12:05PM"
    assert isinstance(params[0], str)
    assert "Jun" in params[0] or "Jun 11" in params[0]
    assert params[1] == "1"
    assert params[3] == 55
    assert conn.commits == 1


def test_repository_marks_online_status():
    # Only "error" should get online=0; stale, missing, ok all get online=1
    for status_name, expected_online in [("ok", "1"), ("stale", "1"), ("missing", "1"), ("error", "0")]:
        conn = FakeConnection()
        repo = SqlServerRepository(conn)
        status = CameraRecordingStatus(
            nvr_name="Main",
            nvr_id=1,
            camera_id="101",
            line_id=1,
            camera_name="Front",
            status=status_name,
            last_start_time=None,
            last_end_time=None,
            age_minutes=None,
            playback_uri=None,
            error=None,
        )

        repo.update_camera_result(status, now=datetime(2026, 6, 11, 12, 6, tzinfo=timezone.utc))

        _, params = conn.cursor_obj.executed[0]
        assert params[1] == expected_online, f"{status_name} should give online={expected_online}"
