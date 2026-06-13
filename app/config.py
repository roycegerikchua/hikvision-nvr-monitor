from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field, field_validator


class CameraConfig(BaseModel):
    id: str = Field(..., description="Hikvision track/channel ID, commonly 101, 201, 301, etc.")
    name: str
    line_id: int | None = None


class NvrConfig(BaseModel):
    name: str
    host: str
    username: str
    password: str
    nvr_id: int | None = None
    port: int = 80
    https: bool = False
    cameras: List[CameraConfig]

    @field_validator("cameras")
    @classmethod
    def require_cameras(cls, value: List[CameraConfig]) -> List[CameraConfig]:
        return value


class AppConfig(BaseModel):
    poll_interval_seconds: int = 300
    lookback_hours: int = 24
    stale_after_minutes: int = 30
    nvrs: List[NvrConfig]

    @field_validator("nvrs")
    @classmethod
    def require_nvrs(cls, value: List[NvrConfig]) -> List[NvrConfig]:
        if not value:
            raise ValueError("configure at least one NVR")
        return value


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    config_path = Path(path or os.getenv("HIKVISION_MONITOR_CONFIG", "config.json"))
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return AppConfig.model_validate(raw)
