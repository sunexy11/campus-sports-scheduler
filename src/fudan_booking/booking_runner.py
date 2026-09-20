from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .booking_api import (
    BookingReadClient,
    BookingSubmission,
    ResourceSummary,
    normalize_time_range,
)
from .models import BlockPreference, SlotKey
from .monitor import _find_resource
from .policy import plan_consecutive_first


def _scheduled_candidates(
    client: BookingReadClient,
    job: dict[str, Any],
    resources: list[ResourceSummary],
    target_date: date,
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
        max_new=int(job.get("max_new_reservations", 1)),
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
) -> dict[str, Any]:
    """Run the configured opening-time strategy once.

    The command is read-only unless ``allow_booking`` is explicitly true.
    Failed optimistic submissions are ordinary results and do not abort lower
    priority attempts or the workflow.
    """

    jobs = [job for job in config.get("scheduled_jobs", []) if job.get("enabled")]
    resources = client.list_resources()
    output: list[dict[str, Any]] = []
    for job in jobs:
        target_date = today + timedelta(days=int(job.get("date_offset", 2)))
        candidates, details = _scheduled_candidates(client, job, resources, target_date)
        item: dict[str, Any] = {
            "name": str(job.get("name") or "scheduled booking"),
            "date": target_date.isoformat(),
            "planned": [slot.time for slot in candidates[: int(job.get("max_new_reservations", 1))]],
            "results": [],
        }
        if not allow_booking:
            item["mode"] = "dry_run"
            output.append(item)
            continue

        item["mode"] = "booking"
        successful = 0
        max_new = int(job.get("max_new_reservations", 1))
        for slot in candidates:
            if successful >= max_new:
                break
            unfinished = client.list_unfinished()
            if len(unfinished) >= 3:
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
                {
                    "venue": slot.venue,
                    "sport": slot.sport,
                    "time": slot.time,
                    "available_sub_resources": len(sub_ids),
                    "ok": result.ok,
                    "reason": result.reason,
                    **({"process_id": result.process_id} if result.process_id else {}),
                }
            )
            if result.ok:
                successful += 1
        item.setdefault("stop_reason", "processed")
        output.append(item)
    return {"mode": "scheduled_book_once", "jobs": output}
