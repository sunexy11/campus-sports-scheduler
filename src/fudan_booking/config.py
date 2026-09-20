from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    raw: dict[str, Any]

    @property
    def max_unfinished_reservations(self) -> int:
        return int(self.raw["limits"]["max_unfinished_reservations"])


def _clock_minutes(value: str, *, allow_24: bool = False) -> int:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid clock value: {value!r}") from exc
    if allow_24 and hour == 24 and minute == 0:
        return 24 * 60
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigurationError(f"invalid clock value: {value!r}")
    return hour * 60 + minute


def load_config(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read config: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML: {exc}") from exc

    if not isinstance(payload, dict):
        raise ConfigurationError("configuration root must be a mapping")
    if payload.get("timezone") != "Asia/Shanghai":
        raise ConfigurationError("timezone must be Asia/Shanghai")

    limits = payload.get("limits")
    if not isinstance(limits, dict):
        raise ConfigurationError("limits must be a mapping")
    if limits.get("max_unfinished_reservations") != 3:
        raise ConfigurationError("max_unfinished_reservations must be 3")
    if limits.get("auto_cancel") is not False:
        raise ConfigurationError("auto_cancel must remain false")

    monitor = payload.get("monitor")
    if not isinstance(monitor, dict):
        raise ConfigurationError("monitor must be a mapping")
    if monitor.get("interval_minutes") != 5:
        raise ConfigurationError("monitor interval must currently be 5 minutes")
    start = _clock_minutes(monitor.get("active_start"))
    end = _clock_minutes(monitor.get("active_end"), allow_24=True)
    if start >= end:
        raise ConfigurationError("monitor active_start must be before active_end")

    notification = payload.get("notification")
    if not isinstance(notification, dict) or notification.get("provider") != "qq_smtp":
        raise ConfigurationError("notification provider must be qq_smtp")

    for job in payload.get("scheduled_jobs", []):
        if job.get("date_offset") != 2:
            raise ConfigurationError("scheduled booking date_offset must be 2")
        if not 0 <= int(job.get("max_new_reservations", 0)) <= 3:
            raise ConfigurationError("max_new_reservations must be between 0 and 3")
        for preference in job.get("preferences", []):
            if not preference.get("venue") or not preference.get("sport"):
                raise ConfigurationError("each preference needs venue and sport")
            if not preference.get("blocks"):
                raise ConfigurationError("each preference needs at least one time block")

    return ProjectConfig(payload)

