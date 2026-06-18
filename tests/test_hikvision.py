from datetime import datetime, timezone, timedelta

import pytest

from app.hikvision import parse_search_response, build_search_payload, classify_camera_status


SAMPLE_SEARCH_RESPONSE = """<?xml version='1.0' encoding='UTF-8'?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <searchID>abc</searchID>
  <responseStatus>true</responseStatus>
  <numOfMatches>2</numOfMatches>
  <matchList>
    <searchMatchItem>
      <sourceID>{00000000-0000-0000-0000-000000000101}</sourceID>
      <trackID>101</trackID>
      <timeSpan>
        <startTime>2026-06-11T08:00:00Z</startTime>
        <endTime>2026-06-11T08:30:00Z</endTime>
      </timeSpan>
      <mediaSegmentDescriptor><playbackURI>rtsp://nvr/old</playbackURI></mediaSegmentDescriptor>
    </searchMatchItem>
    <searchMatchItem>
      <sourceID>{00000000-0000-0000-0000-000000000101}</sourceID>
      <trackID>101</trackID>
      <timeSpan>
        <startTime>2026-06-11T10:00:00Z</startTime>
        <endTime>2026-06-11T10:15:00Z</endTime>
      </timeSpan>
      <mediaSegmentDescriptor><playbackURI>rtsp://nvr/latest</playbackURI></mediaSegmentDescriptor>
    </searchMatchItem>
  </matchList>
</CMSearchResult>
"""


def test_parse_search_response_returns_latest_segment():
    latest = parse_search_response(SAMPLE_SEARCH_RESPONSE)

    assert latest is not None
    assert latest.track_id == "101"
    assert latest.start_time == datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
    assert latest.end_time == datetime(2026, 6, 11, 10, 15, tzinfo=timezone.utc)
    assert latest.playback_uri == "rtsp://nvr/latest"


def test_parse_search_response_returns_none_when_no_matches():
    xml = """<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema"><numOfMatches>0</numOfMatches></CMSearchResult>"""

    assert parse_search_response(xml) is None


def test_build_search_payload_contains_channel_and_time_range():
    start = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc)

    payload = build_search_payload(channel_id="301", start=start, end=end)

    assert "<trackID>301</trackID>" in payload
    assert "2026-06-10T00:00:00Z" in payload
    assert "2026-06-11T00:00:00Z" in payload
    assert "<metadataList>" in payload


@pytest.mark.parametrize(
    "minutes_old, expected",
    [
        (5, "ok"),
        (31, "stale"),
        (None, "missing"),
    ],
)
def test_classify_camera_status(minutes_old, expected):
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    last_end = None if minutes_old is None else now - timedelta(minutes=minutes_old)

    status = classify_camera_status(last_end, now=now, stale_after_minutes=30)

    assert status == expected
