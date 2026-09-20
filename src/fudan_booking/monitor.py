from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .booking_api import (
    BookingReadClient,
    BookingSubmission,
    ResourceSummary,
    normalize_time_range,
)
from .notifier import QQSMTPNotifier


def _target_dates(value: Any, today: date) -> list[date]:
    if value == "next_3_days":
        return [today + timedelta(days=offset) for offset in range(3)]
    if isinstance(value, list):
        dates: list[date] = []
        seen: set[date] = set()
        for item in value:
            # Integer offsets are relative to the run date: 0=today,
            # 1=tomorrow and 2=the day after tomorrow.  Keep accepting
            # explicit ISO dates so existing configurations remain usable.
            if isinstance(item, bool):
                raise TypeError("monitor date offsets must be integers 0, 1 or 2")
            if isinstance(item, int):
                if item not in (0, 1, 2):
                    raise ValueError("monitor date offsets must be integers 0, 1 or 2")
                target = today + timedelta(days=item)
            else:
                target = date.fromisoformat(str(item))
            if target not in seen:
                dates.append(target)
                seen.add(target)
        return dates
    raise ValueError(
        "monitor dates must be next_3_days or a list of offsets (0, 1, 2)"
        " or YYYY-MM-DD dates"
    )


def _max_unfinished_reservations(config: dict[str, Any]) -> int:
    limits = config.get("limits")
    if not isinstance(limits, dict):
        return 3
    return int(limits.get("max_unfinished_reservations", 3))


def _find_resource(resources: list[ResourceSummary], venue: str, sport: str) -> ResourceSummary:
    expected = f"{venue}-{sport}"
    exact = [item for item in resources if item.name.strip() == expected]
    if exact:
        return exact[0]
    matches = [item for item in resources if venue in item.name and sport in item.name]
    if len(matches) != 1:
        raise ValueError(f"cannot uniquely match resource: {expected}")
    return matches[0]


def _scan_monitor_jobs(
    client: BookingReadClient,
    config: dict[str, Any],
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Read one coherent snapshot and keep private IDs for a later submit."""

    resources = client.list_resources()
    findings: list[dict[str, Any]] = []
    for job_index, job in enumerate(config.get("monitor", {}).get("jobs", [])):
        resource = _find_resource(resources, str(job["venue"]), str(job["sport"]))
        wanted_times = {normalize_time_range(str(item)) for item in job.get("times", [])}
        for target_date in _target_dates(job.get("dates"), today):
            availability = client.get_availability(resource.resource_id, target_date)
            for period in availability.periods:
                if normalize_time_range(period.time) not in wanted_times or not period.available:
                    continue
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
                        "_job_index": job_index,
                        "_mode": str(job.get("mode") or "monitor_only"),
                        "_max_new_reservations": int(job.get("max_new_reservations", 1)),
                        "_period_id": period.period_id,
                        "_available_sub_resource_ids": period.available_sub_resource_ids,
                    }
                )
    return findings


def _public_finding(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def monitor_once(
    client: BookingReadClient,
    config: dict[str, Any],
    notifier: QQSMTPNotifier | None = None,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Check configured slots once and optionally send one consolidated email."""

    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    findings = [
        _public_finding(item)
        for item in _scan_monitor_jobs(client, config, today=today)
    ]

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


def monitor_and_book_once(
    client: BookingReadClient,
    config: dict[str, Any],
    notifier: QQSMTPNotifier | None = None,
    *,
    today: date | None = None,
    allow_booking: bool = False,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Monitor and, only with an explicit flag, book configured free slots.

    Every submission is preceded by a fresh unfinished-reservation query. A
    normal race rejection is recorded in ``booking_results`` and the runner
    continues with the remaining candidates. The bounded re-scan allows a
    newly exposed sub-resource to be tried without creating an infinite loop.
    """

    if not allow_booking:
        findings = monitor_once(client, config, notifier, today=today)
        return {"findings": findings, "booking_results": [], "rounds": 1}
    if max_rounds < 1:
        raise ValueError("max_rounds must be positive")

    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    attempted: set[tuple[object, ...]] = set()
    booking_results: list[dict[str, Any]] = []
    latest_internal: list[dict[str, Any]] = []
    successful_by_job: dict[int, int] = {}
    successful_total = 0
    initial_unfinished: int | None = None
    max_unfinished = _max_unfinished_reservations(config)
    stop_reason = "no_findings"

    for round_number in range(1, max_rounds + 1):
        latest_internal = _scan_monitor_jobs(client, config, today=today)
        candidates = [
            item
            for item in latest_internal
            if item["_mode"] == "auto_book_if_capacity"
            and successful_by_job.get(item["_job_index"], 0)
            < item["_max_new_reservations"]
        ]
        if not candidates:
            stop_reason = "no_findings" if not latest_internal else "processed"
            break

        made_progress = False
        for item in candidates:
            job_index = int(item["_job_index"])
            if successful_by_job.get(job_index, 0) >= int(item["_max_new_reservations"]):
                continue
            sub_resource_ids = tuple(item["_available_sub_resource_ids"])
            key = (
                job_index,
                int(item["resource_id"]),
                item["date"],
                int(item["_period_id"]),
                sub_resource_ids,
            )
            if key in attempted:
                continue
            attempted.add(key)
            unfinished_count = len(client.list_unfinished())
            if initial_unfinished is None:
                initial_unfinished = unfinished_count
            effective_unfinished = max(
                unfinished_count,
                initial_unfinished + successful_total,
            )
            if effective_unfinished >= max_unfinished:
                stop_reason = "capacity_reached"
                break
            result: BookingSubmission = client.submit_booking(
                group_id=int(item["resource_id"]),
                sub_resource_ids=sub_resource_ids,
                period_id=int(item["_period_id"]),
                target_date=date.fromisoformat(item["date"]),
                phone="",
                number=1,
            )
            booking_results.append(
                {
                    "name": item["name"],
                    "date": item["date"],
                    "time": item["time"],
                    "available_sub_resources": len(sub_resource_ids),
                    "ok": result.ok,
                    "reason": result.reason,
                    **({"process_id": result.process_id} if result.process_id else {}),
                    **(
                        {"submit_elapsed_ms": result.submit_elapsed_ms}
                        if result.submit_elapsed_ms is not None
                        else {}
                    ),
                    **(
                        {"challenge_elapsed_ms": result.challenge_elapsed_ms}
                        if result.challenge_elapsed_ms is not None
                        else {}
                    ),
                    **(
                        {"challenge_completed": result.challenge_completed}
                        if result.challenge_completed is not None
                        else {}
                    ),
                }
            )
            if result.ok:
                successful_by_job[job_index] = successful_by_job.get(job_index, 0) + 1
                successful_total += 1
                made_progress = True
            if stop_reason == "capacity_reached":
                break

        if stop_reason == "capacity_reached":
            break
        if not made_progress:
            # All currently visible candidates were attempted and rejected;
            # one final scan on the next round is unnecessary and would only
            # repeat the same requests.
            stop_reason = "processed"
            break
    else:
        stop_reason = "max_rounds"

    findings = [_public_finding(item) for item in latest_internal]
    if (findings or booking_results) and notifier is not None:
        lines = ["检测到以下场馆时段有空位：", ""]
        lines.extend(
            f"- {item['name']} | {item['date']} | {item['time']} | "
            f"空余 {item['available_sub_resources']}/{item['total_sub_resources']} 个，"
            f"已占用 {item['occupied_sub_resources']} 个"
            for item in findings
        )
        if booking_results:
            lines.extend(
                ["", "预约尝试："]
                + [
                    f"- {item['date']} {item['time']}："
                    f"{'成功' if item['ok'] else '跳过（' + item['reason'] + '）'}"
                    for item in booking_results
                ]
            )
        notifier.send("复旦场馆空位提醒", "\n".join(lines))
    return {
        "findings": findings,
        "booking_results": booking_results,
        "rounds": round_number if latest_internal else 0,
        "stop_reason": stop_reason,
    }
