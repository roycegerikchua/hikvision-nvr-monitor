from datetime import datetime, timezone, timedelta

import pytest

from app.config import AppConfig, NvrConfig, CameraConfig
from app.monitor import MonitorService
from app.hikvision import RecordingSegment


class FakeClient:
    def __init__(self, results):
        self.results = results

    async def latest_recording(self, *, channel_id, lookback_hours, now=None):
        result = self.results[channel_id]
        if isinstance(result, Exception):
            raise result
        return result


def make_config():
    return AppConfig(
        poll_interval_seconds=60,
        lookback_hours=24,
        stale_after_minutes=30,
        nvrs=[
            NvrConfig(
                name="Main NVR",
                host="192.168.1.10",
                username="admin",
                password="secret",
                cameras=[CameraConfig(id="101", name="Front Door"), CameraConfig(id="201", name="Stock Room")],
            )
        ],
    )


@pytest.mark.asyncio
async def test_monitor_poll_updates_per_camera_status():
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    fresh = RecordingSegment("101", now - timedelta(minutes=10), now - timedelta(minutes=1), "rtsp://fresh")
    stale = RecordingSegment("201", now - timedelta(hours=2), now - timedelta(hours=1), "rtsp://stale")
    service = MonitorService(make_config(), client_factory=lambda nvr: FakeClient({"101": fresh, "201": stale}))

    await service.poll_once(now=now)
    statuses = service.snapshot()

    assert statuses["Main NVR:101"].status == "ok"
    assert statuses["Main NVR:101"].age_minutes == 1
    assert statuses["Main NVR:201"].status == "stale"
    assert statuses["Main NVR:201"].age_minutes == 60


@pytest.mark.asyncio
async def test_monitor_marks_camera_error_without_breaking_other_cameras():
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    fresh = RecordingSegment("201", now - timedelta(minutes=2), now - timedelta(minutes=1), None)
    service = MonitorService(
        make_config(),
        client_factory=lambda nvr: FakeClient({"101": RuntimeError("login failed"), "201": fresh}),
    )

    await service.poll_once(now=now)
    statuses = service.snapshot()

    assert statuses["Main NVR:101"].status == "error"
    assert statuses["Main NVR:101"].error == "login failed"
    assert statuses["Main NVR:201"].status == "ok"
