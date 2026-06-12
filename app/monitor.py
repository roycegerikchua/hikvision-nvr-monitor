from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timezone
from typing import Callable, Dict
from urllib.parse import urlparse, urlunparse

from .config import AppConfig, NvrConfig
from .hikvision import CameraRecordingStatus, HikvisionClient, classify_camera_status

ClientFactory = Callable[[NvrConfig], HikvisionClient]


def default_client_factory(nvr: NvrConfig) -> HikvisionClient:
    return HikvisionClient(
        host=nvr.host,
        port=nvr.port,
        https=nvr.https,
        username=nvr.username,
        password=nvr.password,
        timeout_seconds=float(os.getenv("NVR_HTTP_TIMEOUT_SECONDS", "20")),
    )


def nvr_web_url(nvr: NvrConfig) -> str:
    scheme = "https" if nvr.https else "http"
    default_port = 443 if nvr.https else 80
    port = "" if not nvr.port or nvr.port == default_port else f":{nvr.port}"
    return f"{scheme}://{nvr.host}{port}/"


def embed_credentials(uri: str | None, username: str, password: str) -> str | None:
    """Inject username:password into an RTSP/HTTP URL for one-click VLC playback."""
    if not uri:
        return uri
    try:
        parsed = urlparse(uri)
        netloc = f"{username}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        return uri


class MonitorService:
    def __init__(self, config: AppConfig, client_factory: ClientFactory = default_client_factory, result_repository=None, config_repository=None) -> None:
        self.config = config
        self.client_factory = client_factory
        self.result_repository = result_repository
        self.config_repository = config_repository
        self._statuses: Dict[str, CameraRecordingStatus] = {}
        self._last_poll_started_at: datetime | None = None
        self._last_poll_finished_at: datetime | None = None
        self._stop_event = asyncio.Event()

    @property
    def last_poll_started_at(self) -> datetime | None:
        return self._last_poll_started_at

    @property
    def last_poll_finished_at(self) -> datetime | None:
        return self._last_poll_finished_at

    def snapshot(self) -> Dict[str, CameraRecordingStatus]:
        return dict(self._statuses)

    def overall_status(self) -> str:
        statuses = [status.status for status in self._statuses.values()]
        if not statuses:
            return "unknown"
        if "error" in statuses:
            return "error"
        if "missing" in statuses:
            return "missing"
        if "stale" in statuses:
            return "stale"
        return "ok"

    async def poll_once(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        self._last_poll_started_at = current
        # Reload NVR/camera config from DB every poll so cameras added/removed are picked up
        if self.config_repository is not None:
            self.config = self.config_repository.load_config()
        tasks = []
        # Do not hit every camera on the same NVR at once. Hikvision units can
        # disconnect/reset recording-search requests when too many ISAPI calls
        # arrive simultaneously, which creates false camera errors even though
        # the NVR/camera is reachable manually.
        per_nvr_limit = max(1, int(os.getenv("NVR_MAX_CONCURRENT_PER_NVR", "2")))

        async def poll_with_limit(semaphore: asyncio.Semaphore, nvr: NvrConfig, camera, client):
            async with semaphore:
                return await self._poll_camera(nvr, camera.id, camera.name, camera.line_id, client, current)

        for nvr in self.config.nvrs:
            client = self.client_factory(nvr)
            semaphore = asyncio.Semaphore(per_nvr_limit)
            for camera in nvr.cameras:
                tasks.append(poll_with_limit(semaphore, nvr, camera, client))
        results = await asyncio.gather(*tasks)
        self._statuses = {self._key(item.nvr_name, item.camera_id): item for item in results}
        if self.result_repository is not None:
            self.result_repository.update_many(results, now=current)
        self._last_poll_finished_at = datetime.now(timezone.utc)

    async def _poll_camera(self, nvr: NvrConfig, camera_id: str, camera_name: str, line_id: int | None, client, now: datetime) -> CameraRecordingStatus:
        try:
            # Hikvision returns only the first page of search results. A long 24h search
            # on busy/motion cameras can return old early-day clips even when fresh clips exist.
            # Search a recent window first for accurate stale/ok classification, then fall back
            # to the full configured lookback only to show the last known old recording.
            recent_lookback_hours = max(1, math.ceil((self.config.stale_after_minutes * 2) / 60))
            recent_lookback_hours = min(self.config.lookback_hours, recent_lookback_hours)
            max_attempts = max(1, int(os.getenv("NVR_POLL_RETRY_ATTEMPTS", "2")))
            last_exc = None
            segment = None
            for attempt in range(max_attempts):
                try:
                    segment = await client.latest_recording(
                        channel_id=camera_id,
                        lookback_hours=recent_lookback_hours,
                        now=now,
                    )
                    if segment is None and recent_lookback_hours < self.config.lookback_hours:
                        segment = await client.latest_recording(
                            channel_id=camera_id,
                            lookback_hours=self.config.lookback_hours,
                            now=now,
                        )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(1)
            if last_exc is not None:
                raise last_exc
            last_end_time = segment.end_time if segment else None
            age = None
            if last_end_time is not None:
                age = round((now - last_end_time).total_seconds() / 60, 1)
                # NVR clocks can be ahead of the monitor (clock skew / timezone mismatch).
                # Clamp negative ages to 0 so the UI doesn't show future timestamps.
                if age < 0:
                    age = 0
            return CameraRecordingStatus(
                nvr_name=nvr.name,
                camera_id=camera_id,
                camera_name=camera_name,
                status=classify_camera_status(last_end_time, now=now, stale_after_minutes=self.config.stale_after_minutes),
                last_start_time=segment.start_time if segment else None,
                last_end_time=last_end_time,
                age_minutes=age,
                playback_uri=embed_credentials(segment.playback_uri, nvr.username, nvr.password) if segment else None,
                nvr_host=nvr.host,
                nvr_web_url=nvr_web_url(nvr),
                nvr_id=nvr.nvr_id,
                line_id=line_id,
            )
        except Exception as exc:  # keep one failed camera/NVR from hiding the rest
            return CameraRecordingStatus(
                nvr_name=nvr.name,
                camera_id=camera_id,
                camera_name=camera_name,
                status="error",
                last_start_time=None,
                last_end_time=None,
                age_minutes=None,
                playback_uri=None,
                nvr_host=nvr.host,
                nvr_web_url=nvr_web_url(nvr),
                error=(str(exc) or repr(exc) or type(exc).__name__),
                nvr_id=nvr.nvr_id,
                line_id=line_id,
            )

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _key(nvr_name: str, camera_id: str) -> str:
        return f"{nvr_name}:{camera_id}"
