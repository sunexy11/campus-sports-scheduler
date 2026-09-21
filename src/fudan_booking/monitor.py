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


def _unfinished_count(client: BookingReadClient) -> int:
    """Read the account-wide unfinished count from the booking site.

    A small fallback keeps the pure read-only test doubles backwards
    compatible; the real client always implements ``list_unfinished``.
    """

    reader = getattr(client, "list_unfinished", None)
    return len(reader()) if callable(reader) else 0


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
        # max_new_reservations=0 explicitly disables this job.  This avoids
        # polling a target that the user has no remaining budget to book.
        max_new = max(0, int(job.get("max_new_reservations", 1)))
        if max_new == 0:
            continue
        resource = _find_resource(resources, str(job["venue"]), str(job["sport"]))
        wanted_times = {normalize_time_range(str(item)) for item in job.get("times", [])}
        for target_date in _target_dates(job.get("dates"), today):
            availability = client.get_availability(resource.resource_id, target_date)
            periods_by_time = {
                normalize_time_range(period.time): period
                for period in availability.periods
            }
            for wanted_time in sorted(wanted_times):
                period = periods_by_time.get(wanted_time)
                if period is None:
                    # Keep a configured target visible in the report even if
                    # the API did not return that period in this snapshot.
                    period_id = None
                    available_count = 0
                    total_count = 0
                    available_ids: tuple[int, ...] = ()
                    display_time = wanted_time
                else:
                    period_id = period.period_id
                    available_count = period.available_sub_resources
                    total_count = period.total_sub_resources
                    available_ids = period.available_sub_resource_ids
                    display_time = period.time
                findings.append(
                    {
                        "name": str(job.get("name") or resource.name),
                        "resource_id": resource.resource_id,
                        "date": target_date.isoformat(),
                        "time": display_time,
                        "available_sub_resources": available_count,
                        "occupied_sub_resources": (
                            total_count - available_count
                        ),
                        "total_sub_resources": total_count,
                        "_available": bool(period and period.available),
                        "_job_index": job_index,
                        "_mode": str(job.get("mode") or "monitor_only"),
                        "_max_new_reservations": max_new,
                        "_period_id": period_id,
                        "_available_sub_resource_ids": available_ids,
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
    jobs = [
        job
        for job in config.get("monitor", {}).get("jobs", [])
        if max(0, int(job.get("max_new_reservations", 1))) > 0
    ]
    if not jobs:
        return []
    unfinished_count = _unfinished_count(client)
    max_unfinished = _max_unfinished_reservations(config)
    if unfinished_count >= max_unfinished:
        return []

    internal = _scan_monitor_jobs(client, config, today=today)
    findings = [
        _public_finding(item) for item in internal if item.get("_available")
    ]

    if findings and notifier is not None:
        lines = ["复旦场馆空位提醒：", ""]
        lines.extend(
            f"- {item['name']} | {item['date']} | {item['time']} | "
            f"空余 {item['available_sub_resources']}/{item['total_sub_resources']} 个"
            for item in findings
        )
        notifier.send("复旦场馆空位监控汇总", "\n".join(lines))
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

    if max_rounds < 1:
        raise ValueError("max_rounds must be positive")

    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    configured_jobs = [
        job
        for job in config.get("monitor", {}).get("jobs", [])
        if max(0, int(job.get("max_new_reservations", 1))) > 0
    ]
    if not configured_jobs:
        return {
            "findings": [],
            "target_slots": [],
            "booking_results": [],
            "rounds": 0,
            "stop_reason": "no_monitorable_jobs",
        }

    initial_unfinished = _unfinished_count(client)
    max_unfinished = _max_unfinished_reservations(config)
    remaining_capacity = max(0, max_unfinished - initial_unfinished)
    if remaining_capacity <= 0:
        return {
            "findings": [],
            "target_slots": [],
            "booking_results": [],
            "rounds": 0,
            "stop_reason": "capacity_reached",
            "unfinished_reservation_count": initial_unfinished,
            "remaining_capacity": 0,
        }

    if not allow_booking:
        latest_internal = _scan_monitor_jobs(client, config, today=today)
        target_slots = [_public_finding(item) for item in latest_internal]
        findings = [
            _public_finding(item)
            for item in latest_internal
            if item.get("_available")
        ]
        for public_item in target_slots:
            public_item["booking_status"] = (
                "无空位"
                if not public_item["available_sub_resources"]
                else "未开启自动预约"
            )
        if findings and notifier is not None:
            lines = ["复旦场馆空位提醒：", ""]
            lines.extend(
                f"- {item['name']} | {item['date']} | {item['time']} | "
                f"空余 {item['available_sub_resources']}/{item['total_sub_resources']} 个"
                for item in findings
            )
            notifier.send("复旦场馆空位监控汇总", "\n".join(lines))
        return {
            "findings": findings,
            "target_slots": target_slots,
            "booking_results": [],
            "rounds": 1 if latest_internal else 0,
            "stop_reason": "processed" if latest_internal else "no_findings",
            "unfinished_reservation_count": initial_unfinished,
            "remaining_capacity": remaining_capacity,
        }

    attempted: set[tuple[object, ...]] = set()
    booking_results: list[dict[str, Any]] = []
    latest_internal: list[dict[str, Any]] = []
    successful_by_job: dict[int, int] = {}
    successful_total = 0
    stop_reason = "no_findings"

    for round_number in range(1, max_rounds + 1):
        latest_internal = _scan_monitor_jobs(client, config, today=today)
        candidates = [
            item
            for item in latest_internal
            if item.get("_available")
            and item["_mode"] == "auto_book_if_capacity"
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
            unfinished_count = _unfinished_count(client)
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
                    "_job_index": job_index,
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
                        {"first_post_elapsed_ms": result.first_post_elapsed_ms}
                        if result.first_post_elapsed_ms is not None
                        else {}
                    ),
                    **(
                        {"retry_post_elapsed_ms": result.retry_post_elapsed_ms}
                        if result.retry_post_elapsed_ms is not None
                        else {}
                    ),
                    **(
                        {"final_post_elapsed_ms": result.final_post_elapsed_ms}
                        if result.final_post_elapsed_ms is not None
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
                    **(
                        {
                            "challenge_completion_signal": result.challenge_completion_signal
                        }
                        if result.challenge_completion_signal is not None
                        else {}
                    ),
                    **(
                        {
                            "retry_challenge_elapsed_ms": result.retry_challenge_elapsed_ms
                        }
                        if result.retry_challenge_elapsed_ms is not None
                        else {}
                    ),
                    **(
                        {
                            "retry_challenge_completed": result.retry_challenge_completed
                        }
                        if result.retry_challenge_completed is not None
                        else {}
                    ),
                    **(
                        {
                            "retry_challenge_completion_signal": result.retry_challenge_completion_signal
                        }
                        if result.retry_challenge_completion_signal is not None
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

    target_slots = [_public_finding(item) for item in latest_internal]
    findings = [
        _public_finding(item)
        for item in latest_internal
        if item.get("_available")
    ]
    if findings and notifier is not None:
        result_by_slot = {
            (item["_job_index"], item["date"], item["time"]): item
            for item in booking_results
        }
        lines = ["复旦场馆监控及预约汇总："]
        successful = [item for item in booking_results if item["ok"]]
        if successful:
            lines.extend(["", "本次成功预约："])
            lines.extend(
                f"- {item['name']} | {item['date']} | {item['time']}"
                for item in successful
            )
        lines.extend(["", "发现空位："])
        for internal_item, item in zip(latest_internal, target_slots):
            if not internal_item.get("_available"):
                continue
            key = (internal_item["_job_index"], item["date"], item["time"])
            attempt = result_by_slot.get(key)
            if not item["available_sub_resources"]:
                status = "无空位"
            elif attempt is not None:
                status = "预约成功" if attempt["ok"] else f"预约未成功（{attempt['reason']}）"
            elif internal_item["_mode"] != "auto_book_if_capacity":
                status = "仅监控"
            else:
                status = "未尝试"
            item["booking_status"] = status
            lines.append(
                f"- {item['name']} | {item['date']} | {item['time']} | "
                f"空余 {item['available_sub_resources']}/{item['total_sub_resources']} 个 | "
                f"预约状态：{status}"
            )
        notifier.send("复旦场馆监控及预约汇总", "\n".join(lines))
    return {
        "findings": findings,
        "target_slots": target_slots,
        "booking_results": [_public_finding(item) for item in booking_results],
        "rounds": round_number if latest_internal else 0,
        "stop_reason": stop_reason,
        "unfinished_reservation_count": initial_unfinished,
        "remaining_capacity": remaining_capacity,
    }
