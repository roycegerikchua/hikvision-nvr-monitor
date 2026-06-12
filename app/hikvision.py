from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from xml.etree import ElementTree as ET

import httpx


@dataclass(frozen=True)
class RecordingSegment:
    track_id: str
    start_time: datetime
    end_time: datetime
    playback_uri: Optional[str] = None


@dataclass(frozen=True)
class Camera:
    id: str
    name: str


@dataclass(frozen=True)
class CameraRecordingStatus:
    nvr_name: str
    camera_id: str
    camera_name: str
    status: str
    last_start_time: Optional[datetime]
    last_end_time: Optional[datetime]
    age_minutes: Optional[float]
    playback_uri: Optional[str]
    nvr_host: Optional[str] = None
    nvr_web_url: Optional[str] = None
    error: Optional[str] = None
    nvr_id: Optional[int] = None
    line_id: Optional[int] = None


def _strip_namespace(element: ET.Element) -> None:
    for node in element.iter():
        if "}" in node.tag:
            node.tag = node.tag.rsplit("}", 1)[1]


def parse_hikvision_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _text(parent: ET.Element, path: str) -> Optional[str]:
    node = parent.find(path)
    return node.text.strip() if node is not None and node.text else None


def parse_search_response(xml_text: str) -> Optional[RecordingSegment]:
    root = ET.fromstring(xml_text)
    _strip_namespace(root)
    matches: list[RecordingSegment] = []

    for item in root.findall(".//searchMatchItem"):
        track_id = _text(item, "trackID") or ""
        start_raw = _text(item, "timeSpan/startTime")
        end_raw = _text(item, "timeSpan/endTime")
        if not start_raw or not end_raw:
            continue
        matches.append(
            RecordingSegment(
                track_id=track_id,
                start_time=parse_hikvision_datetime(start_raw),
                end_time=parse_hikvision_datetime(end_raw),
                playback_uri=_text(item, "mediaSegmentDescriptor/playbackURI"),
            )
        )

    if not matches:
        return None
    return max(matches, key=lambda match: match.end_time)


def _hikvision_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_search_payload(channel_id: str, start: datetime, end: datetime, *, position: int = 0, max_results: int = 64) -> str:
    search_id = str(uuid4()).upper()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
  <searchID>{search_id}</searchID>
  <trackList><trackID>{channel_id}</trackID></trackList>
  <timeSpanList>
    <timeSpan>
      <startTime>{_hikvision_time(start)}</startTime>
      <endTime>{_hikvision_time(end)}</endTime>
    </timeSpan>
  </timeSpanList>
  <maxResults>{max_results}</maxResults>
  <searchResultPostion>{position}</searchResultPostion>
  <metadataList>
    <metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
  </metadataList>
</CMSearchDescription>"""


def classify_camera_status(
    last_end_time: Optional[datetime], *, now: datetime, stale_after_minutes: int
) -> str:
    if last_end_time is None:
        return "missing"
    age = now.astimezone(timezone.utc) - last_end_time.astimezone(timezone.utc)
    return "stale" if age > timedelta(minutes=stale_after_minutes) else "ok"


class HikvisionClient:
    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        https: bool = False,
        timeout_seconds: float = 10,
    ) -> None:
        scheme = "https" if https else "http"
        self.base_url = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
        self.auth = httpx.DigestAuth(username, password)
        self.timeout_seconds = timeout_seconds

    async def latest_recording(
        self,
        *,
        channel_id: str,
        lookback_hours: int,
        now: Optional[datetime] = None,
    ) -> Optional[RecordingSegment]:
        current = now or datetime.now(timezone.utc)
        start = current - timedelta(hours=lookback_hours)
        page_size = 64
        position = 0
        all_segments: list[RecordingSegment] = []
        async with httpx.AsyncClient(auth=self.auth, timeout=self.timeout_seconds, verify=False) as client:
            while True:
                payload = build_search_payload(
                    channel_id=channel_id, start=start, end=current,
                    position=position, max_results=page_size,
                )
                response = await client.post(
                    f"{self.base_url}/ISAPI/ContentMgmt/search",
                    content=payload.encode("utf-8"),
                    headers={"Content-Type": "application/xml"},
                )
                response.raise_for_status()
                # Parse matches from this page
                root = ET.fromstring(response.text)
                _strip_namespace(root)
                matches: list[RecordingSegment] = []
                for item in root.findall(".//searchMatchItem"):
                    track_id = _text(item, "trackID") or ""
                    start_raw = _text(item, "timeSpan/startTime")
                    end_raw = _text(item, "timeSpan/endTime")
                    if not start_raw or not end_raw:
                        continue
                    matches.append(
                        RecordingSegment(
                            track_id=track_id,
                            start_time=parse_hikvision_datetime(start_raw),
                            end_time=parse_hikvision_datetime(end_raw),
                            playback_uri=_text(item, "mediaSegmentDescriptor/playbackURI"),
                        )
                    )
                if not matches:
                    break
                all_segments.extend(matches)
                if len(matches) < page_size:
                    break
                position += page_size
                if position > page_size * 10:
                    break
        if not all_segments:
            return None
        return max(all_segments, key=lambda m: m.end_time)
