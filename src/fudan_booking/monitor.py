from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .booking_api import BookingReadClient, ResourceSummary
from .notifier import QQSMTPNotifier


def _target_dates(value: Any, today: date) -> list[date]:
    if value == "next_3_days":
        return [today + timedelta(days=offset) for offset in range(3)]
    if isinstance(value, list):
        return [date.fromisoformat(str(item)) for item in value]
    raise ValueError("monitor dates must be next_3_days or a list of YYYY-MM-DD")


def _find_resource(resources: list[ResourceSummary], venue: str, sport: str) -> ResourceSummary:
    expected = f"{venue}-{sport}"
    exact = [item for item in resources if item.name.strip() == expected]
    if exact:
        return exact[0]
    matches = [item for item in resources if venue in item.name and sport in item.name]
    if len(matches) != 1:
        raise ValueError(f"cannot uniquely match resource: {expected}")
    return matches[0]


def monitor_once(
    client: BookingReadClient,
    config: dict[str, Any],
    notifier: QQSMTPNotifier | None = None,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Check configured slots once and optionally send one consolidated email."""

    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    resources = client.list_resources()
    findings: list[dict[str, Any]] = []
    for job in config.get("monitor", {}).get("jobs", []):
        resource = _find_resource(resources, str(job["venue"]), str(job["sport"]))
        wanted_times = {str(item) for item in job.get("times", [])}
        for target_date in _target_dates(job.get("dates"), today):
            availability = client.get_availability(resource.resource_id, target_date)
            for period in availability.periods:
                if period.time in wanted_times and period.available:
                    findings.append(
                        {
                            "name": str(job.get("name") or resource.name),
                            "resource_id": resource.resource_id,
                            "date": target_date.isoformat(),
                            "time": period.time,
                            "available_sub_resources": period.available_sub_resources,
                            "occupied_sub_resources": (
                                period.total_sub_resources - period.available_sub_resources
                            ),
                            "total_sub_resources": period.total_sub_resources,
                        }
                    )

    if findings and notifier is not None:
        lines = ["检测到以下场馆时段有空位：", ""]
        lines.extend(
            f"- {item['name']} | {item['date']} | {item['time']} | "
            f"空余 {item['available_sub_resources']}/{item['total_sub_resources']} 个，"
            f"已占用 {item['occupied_sub_resources']} 个"
            for item in findings
        )
        notifier.send("复旦场馆空位提醒", "\n".join(lines))
    return findings
