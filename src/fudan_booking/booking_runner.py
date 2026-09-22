from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import requests

from .booking_api import (
    BookingReadClient,
    BookingSubmission,
    ResourceSummary,
    normalize_time_range,
)
from .errors import AuthenticationExpired, BookingError
from .models import BlockPreference, SlotKey
from .monitor import _find_resource
from .policy import plan_consecutive_first


def _submission_output(
    slot: SlotKey,
    available_sub_resources: int,
    result: BookingSubmission,
) -> dict[str, Any]:
    return {
        "venue": slot.venue,
        "sport": slot.sport,
        "time": slot.time,
        "available_sub_resources": available_sub_resources,
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
            {"challenge_completion_signal": result.challenge_completion_signal}
            if result.challenge_completion_signal is not None
            else {}
        ),
        **(
            {"retry_challenge_elapsed_ms": result.retry_challenge_elapsed_ms}
            if result.retry_challenge_elapsed_ms is not None
            else {}
        ),
        **(
            {"retry_challenge_completed": result.retry_challenge_completed}
            if result.retry_challenge_completed is not None
            else {}
        ),
        **(
            {
                "retry_challenge_completion_signal": (
                    result.retry_challenge_completion_signal
                )
            }
            if result.retry_challenge_completion_signal is not None
            else {}
        ),
    }


def prewarm_scheduled_context(
    client: BookingReadClient,
    config: dict[str, Any],
    *,
    today: date,
    resources: list[ResourceSummary],
) -> None:
    """Warm target calendar requests before the opening-time wait.

    Calendar data is deliberately not reused for booking: periods can open or
    disappear at 07:00.  The final strategy pass refreshes it after the wait;
    this pass only warms the persistent bridge, DNS/TLS connection and parser.
    """

    warmed: set[tuple[int, date]] = set()
    for job in config.get("scheduled_jobs", []):
        if not job.get("enabled") or max(0, int(job.get("max_new_reservations", 1))) == 0:
            continue
        target_date = today + timedelta(days=int(job.get("date_offset", 2)))
        for preference in job.get("preferences", []):
            resource = _find_resource(
                resources,
                str(preference["venue"]),
                str(preference["sport"]),
            )
            key = (resource.resource_id, target_date)
            if key in warmed:
                continue
            client.get_availability(resource.resource_id, target_date)
            warmed.add(key)


def _scheduled_candidates(
    client: BookingReadClient,
    job: dict[str, Any],
    resources: list[ResourceSummary],
    target_date: date,
    *,
    max_new: int | None = None,
) -> tuple[list[SlotKey], dict[SlotKey, tuple[int, int, tuple[int, ...]]]]:
    preferences: list[BlockPreference] = []
    availability_by_key: dict[SlotKey, tuple[int, int, tuple[int, ...]]] = {}
    for priority, preference in enumerate(job.get("preferences", [])):
        venue = str(preference["venue"])
        sport = str(preference["sport"])
        preferences.extend(
            BlockPreference(
                venue,
                sport,
                tuple(normalize_time_range(str(item)) for item in block),
                priority,
            )
            for block in preference.get("blocks", [])
        )
        resource = _find_resource(resources, venue, sport)
        availability = client.get_availability(resource.resource_id, target_date)
        by_time = {
            normalize_time_range(period.time): period for period in availability.periods
        }
        for block in preference.get("blocks", []):
            for raw_time in block:
                time = normalize_time_range(str(raw_time))
                period = by_time.get(time)
                if period is None or not period.available:
                    continue
                key = SlotKey(venue, sport, target_date, time)
                availability_by_key[key] = (
                    resource.resource_id,
                    period.period_id,
                    period.available_sub_resource_ids,
                )

    available = set(availability_by_key)
    # Keep the policy's consecutive-block selection first, then append every
    # lower-priority available slot as a race fallback.
    plan = plan_consecutive_first(
        target_date=target_date,
        preferences=preferences,
        available=available,
        unfinished=0,
        maximum=3,
        max_new=(
            int(job.get("max_new_reservations", 1))
            if max_new is None
            else max_new
        ),
        fallback_to_single=bool(job.get("fallback_to_single_slot", True)),
    )
    ordered = list(plan.selected)
    selected = set(ordered)
    for preference in sorted(preferences, key=lambda item: item.priority):
        for time in preference.times:
            key = SlotKey(preference.venue, preference.sport, target_date, time)
            if key in available and key not in selected:
                ordered.append(key)
                selected.add(key)
    return ordered, availability_by_key


def scheduled_book_once(
    client: BookingReadClient,
    config: dict[str, Any],
    *,
    today: date,
    allow_booking: bool,
    prepared_resources: list[ResourceSummary] | None = None,
) -> dict[str, Any]:
    """Run the configured opening-time strategy once.

    The command is read-only unless ``allow_booking`` is explicitly true.
    Failed optimistic submissions are ordinary results and do not abort lower
    priority attempts or the workflow.
    """

    jobs = [job for job in config.get("scheduled_jobs", []) if job.get("enabled")]
    output: list[dict[str, Any]] = []
    limits = config.get("limits")
    max_unfinished = (
        int(limits.get("max_unfinished_reservations", 3))
        if isinstance(limits, dict)
        else 3
    )
    successful_total = 0
    # This is deliberately read from the site's reservation list, rather than
    # inferred from the local config.  One account-wide limit is shared by all
    # scheduled jobs in this run.
    needs_capacity_check = any(
        max(0, int(job.get("max_new_reservations", 1))) > 0 for job in jobs
    )
    initial_unfinished = len(client.list_unfinished()) if needs_capacity_check else 0
    remaining_capacity = max(0, max_unfinished - initial_unfinished)
    resources: list[ResourceSummary] | None = prepared_resources
    for job in jobs:
        target_date = today + timedelta(days=int(job.get("date_offset", 2)))
        configured_max_new = max(0, int(job.get("max_new_reservations", 1)))
        budget = min(
            configured_max_new,
            max(0, max_unfinished - (initial_unfinished + successful_total)),
        )
        item: dict[str, Any] = {
            "name": str(job.get("name") or "scheduled booking"),
            "date": target_date.isoformat(),
            "planned": [],
            "results": [],
        }
        if configured_max_new == 0:
            item["mode"] = "booking" if allow_booking else "dry_run"
            item["stop_reason"] = "max_new_reservations_zero"
            output.append(item)
            continue
        if budget <= 0:
            item["mode"] = "booking" if allow_booking else "dry_run"
            item["stop_reason"] = "capacity_reached"
            output.append(item)
            continue

        if resources is None:
            resources = client.list_resources()
        candidates, details = _scheduled_candidates(
            client, job, resources, target_date, max_new=budget
        )
        item["planned"] = [slot.time for slot in candidates[:budget]]
        if not allow_booking:
            item["mode"] = "dry_run"
            output.append(item)
            continue

        item["mode"] = "booking"
        successful = 0
        for slot in candidates:
            if successful >= budget:
                break
            unfinished_count = len(client.list_unfinished())
            effective_unfinished = max(
                unfinished_count,
                initial_unfinished + successful_total,
            )
            if effective_unfinished >= max_unfinished:
                item["stop_reason"] = "capacity_reached"
                break
            resource_id, period_id, sub_ids = details[slot]
            if not sub_ids:
                continue
            result: BookingSubmission = client.submit_booking(
                group_id=resource_id,
                sub_resource_ids=sub_ids,
                period_id=period_id,
                target_date=slot.date,
                phone="",
                number=1,
            )
            item["results"].append(
                _submission_output(slot, len(sub_ids), result)
            )
            if result.ok:
                successful += 1
                successful_total += 1
        item.setdefault("stop_reason", "processed")
        output.append(item)
    return {
        "mode": "scheduled_book_once",
        "unfinished_reservation_count": initial_unfinished,
        "remaining_capacity": remaining_capacity,
        "jobs": output,
    }


def _opening_retry_delay(elapsed_seconds: float, consecutive_errors: int) -> float:
    """Use a fast opening burst, then back off without hammering the site."""

    if consecutive_errors:
        return float(min(5, 2 ** min(consecutive_errors - 1, 3)))
    if elapsed_seconds < 20:
        return 0.75
    if elapsed_seconds < 60:
        return 1.0
    return 2.0


def scheduled_book_with_retries(
    client: BookingReadClient,
    config: dict[str, Any],
    *,
    today: date,
    allow_booking: bool,
    prepared_resources: list[ResourceSummary] | None = None,
    retry_window_seconds: int = 180,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Keep the opening-time strategy alive for a bounded relative window.

    The authenticated client and its Node/JSDOM cookie jar are reused for the
    complete run.  No re-login is attempted.  Temporary read/submit failures
    are retried, while an ambiguous submit is reconciled against the account's
    unfinished-reservation count before that slot may be submitted again.
    """

    if not allow_booking:
        return scheduled_book_once(
            client,
            config,
            today=today,
            allow_booking=False,
            prepared_resources=prepared_resources,
        )
    if retry_window_seconds <= 0:
        raise ValueError("retry_window_seconds must be positive")

    jobs = [job for job in config.get("scheduled_jobs", []) if job.get("enabled")]
    limits = config.get("limits")
    max_unfinished = (
        int(limits.get("max_unfinished_reservations", 3))
        if isinstance(limits, dict)
        else 3
    )
    output: list[dict[str, Any]] = []
    configured_limits: list[int] = []
    for job in jobs:
        configured_max = max(0, int(job.get("max_new_reservations", 1)))
        configured_limits.append(configured_max)
        output.append(
            {
                "name": str(job.get("name") or "scheduled booking"),
                "date": (
                    today + timedelta(days=int(job.get("date_offset", 2)))
                ).isoformat(),
                "planned": [],
                "results": [],
                "mode": "booking",
                **(
                    {"stop_reason": "max_new_reservations_zero"}
                    if configured_max == 0
                    else {}
                ),
            }
        )

    started_at = monotonic()
    deadline = started_at + retry_window_seconds
    resources = prepared_resources
    initial_unfinished: int | None = None
    latest_unfinished = 0
    successful_by_job = [0 for _ in jobs]
    successful_total = 0
    completed_slots: set[SlotKey] = set()
    permanent_slots: set[SlotKey] = set()
    pending_submit: dict[str, Any] | None = None
    transient_errors: list[dict[str, Any]] = []
    transient_error_count = 0
    consecutive_errors = 0
    rounds = 0
    stop_reason = "retry_window_elapsed"

    def record_transient(stage: str, exc: BaseException) -> None:
        nonlocal transient_error_count
        transient_error_count += 1
        transient_errors.append(
            {
                "round": rounds,
                "stage": stage,
                "error_type": type(exc).__name__,
            }
        )
        del transient_errors[:-20]

    def wait_for_next_round() -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        elapsed = monotonic() - started_at
        sleeper(min(remaining, _opening_retry_delay(elapsed, consecutive_errors)))

    while monotonic() < deadline:
        rounds += 1
        try:
            current_unfinished = len(client.list_unfinished())
        except AuthenticationExpired as exc:
            record_transient("unfinished_reservation_list", exc)
            stop_reason = "session_expired"
            break
        except (BookingError, requests.RequestException) as exc:
            record_transient("unfinished_reservation_list", exc)
            consecutive_errors += 1
            wait_for_next_round()
            continue

        consecutive_errors = 0
        latest_unfinished = current_unfinished
        if initial_unfinished is None:
            initial_unfinished = current_unfinished

        # A transport failure after POST is ambiguous: the server may have
        # accepted the booking before the response was lost.  Require two
        # successful account-list reads before retrying that slot.
        if pending_submit is not None:
            if current_unfinished > int(pending_submit["unfinished_before"]):
                pending_submit["result"]["ok"] = True
                pending_submit["result"]["reason"] = "submitted_unconfirmed"
                job_index = int(pending_submit["job_index"])
                slot = pending_submit["slot"]
                successful_by_job[job_index] += 1
                successful_total += 1
                completed_slots.add(slot)
                pending_submit = None
            elif int(pending_submit["checks"]) < 1:
                pending_submit["checks"] = int(pending_submit["checks"]) + 1
                wait_for_next_round()
                continue
            else:
                pending_submit["result"]["reason"] = "transient_error"
                pending_submit = None

        effective_unfinished = max(
            current_unfinished,
            initial_unfinished + successful_total,
        )
        if effective_unfinished >= max_unfinished:
            stop_reason = "capacity_reached"
            break
        if all(
            limit == 0 or successful_by_job[index] >= limit
            for index, limit in enumerate(configured_limits)
        ):
            stop_reason = "max_new_reservations_reached"
            break

        if resources is None:
            try:
                resources = client.list_resources()
            except AuthenticationExpired as exc:
                record_transient("resource_list", exc)
                stop_reason = "session_expired"
                break
            except (BookingError, requests.RequestException) as exc:
                record_transient("resource_list", exc)
                consecutive_errors += 1
                wait_for_next_round()
                continue

        round_interrupted = False
        for job_index, (job, item) in enumerate(zip(jobs, output)):
            configured_max = configured_limits[job_index]
            remaining_for_job = configured_max - successful_by_job[job_index]
            if remaining_for_job <= 0:
                item["stop_reason"] = "max_new_reservations_reached"
                continue
            account_remaining = max_unfinished - max(
                latest_unfinished,
                initial_unfinished + successful_total,
            )
            if account_remaining <= 0:
                stop_reason = "capacity_reached"
                round_interrupted = True
                break
            budget = min(remaining_for_job, account_remaining)
            target_date = today + timedelta(days=int(job.get("date_offset", 2)))
            try:
                candidates, details = _scheduled_candidates(
                    client,
                    job,
                    resources,
                    target_date,
                    max_new=budget,
                )
            except AuthenticationExpired as exc:
                record_transient("resource_calendar", exc)
                stop_reason = "session_expired"
                round_interrupted = True
                break
            except (BookingError, requests.RequestException) as exc:
                record_transient("resource_calendar", exc)
                consecutive_errors += 1
                round_interrupted = True
                break

            for slot in candidates[:budget]:
                if slot.time not in item["planned"]:
                    item["planned"].append(slot.time)

            for slot in candidates:
                if monotonic() >= deadline:
                    round_interrupted = True
                    break
                if successful_by_job[job_index] >= configured_max:
                    item["stop_reason"] = "max_new_reservations_reached"
                    break
                if slot in completed_slots or slot in permanent_slots:
                    continue

                try:
                    unfinished_before = len(client.list_unfinished())
                except AuthenticationExpired as exc:
                    record_transient("pre_submit_reservation_list", exc)
                    stop_reason = "session_expired"
                    round_interrupted = True
                    break
                except (BookingError, requests.RequestException) as exc:
                    record_transient("pre_submit_reservation_list", exc)
                    consecutive_errors += 1
                    round_interrupted = True
                    break

                latest_unfinished = unfinished_before
                effective_unfinished = max(
                    unfinished_before,
                    initial_unfinished + successful_total,
                )
                if effective_unfinished >= max_unfinished:
                    stop_reason = "capacity_reached"
                    round_interrupted = True
                    break
                resource_id, period_id, sub_ids = details[slot]
                if not sub_ids:
                    continue
                try:
                    result = client.submit_booking(
                        group_id=resource_id,
                        sub_resource_ids=sub_ids,
                        period_id=period_id,
                        target_date=slot.date,
                        phone="",
                        number=1,
                    )
                except AuthenticationExpired as exc:
                    record_transient("booking_submit", exc)
                    stop_reason = "session_expired"
                    round_interrupted = True
                    break
                except (BookingError, requests.RequestException) as exc:
                    record_transient("booking_submit", exc)
                    uncertain_result = {
                        "venue": slot.venue,
                        "sport": slot.sport,
                        "time": slot.time,
                        "available_sub_resources": len(sub_ids),
                        "ok": False,
                        "reason": "submission_outcome_unknown",
                        "error_type": type(exc).__name__,
                    }
                    item["results"].append(uncertain_result)
                    pending_submit = {
                        "job_index": job_index,
                        "slot": slot,
                        "unfinished_before": unfinished_before,
                        "checks": 0,
                        "result": uncertain_result,
                    }
                    consecutive_errors += 1
                    round_interrupted = True
                    break

                item["results"].append(
                    _submission_output(slot, len(sub_ids), result)
                )
                if result.ok:
                    successful_by_job[job_index] += 1
                    successful_total += 1
                    completed_slots.add(slot)
                    consecutive_errors = 0
                elif result.reason in {
                    "overlap_with_existing",
                    "existing_reservation",
                }:
                    permanent_slots.add(slot)

            if round_interrupted:
                break

        if stop_reason in {"capacity_reached", "session_expired"}:
            break
        if all(
            limit == 0 or successful_by_job[index] >= limit
            for index, limit in enumerate(configured_limits)
        ):
            stop_reason = "max_new_reservations_reached"
            break
        wait_for_next_round()

    effective_initial = initial_unfinished if initial_unfinished is not None else 0
    effective_latest = max(
        latest_unfinished,
        effective_initial + successful_total,
    )
    for index, item in enumerate(output):
        if "stop_reason" in item:
            continue
        if successful_by_job[index] >= configured_limits[index]:
            item["stop_reason"] = "max_new_reservations_reached"
        else:
            item["stop_reason"] = stop_reason

    return {
        "mode": "scheduled_book_retry",
        "retry_window_seconds": retry_window_seconds,
        "rounds": rounds,
        "stop_reason": stop_reason,
        "unfinished_reservation_count": initial_unfinished,
        "remaining_capacity": max(0, max_unfinished - effective_latest),
        "transient_error_count": transient_error_count,
        "transient_errors": transient_errors,
        "jobs": output,
    }
