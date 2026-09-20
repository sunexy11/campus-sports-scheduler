from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from .models import BlockPreference, BookingPlan, SlotKey


def remaining_capacity(unfinished: int, maximum: int = 3) -> int:
    if unfinished < 0:
        raise ValueError("unfinished reservation count cannot be negative")
    if maximum < 1:
        raise ValueError("maximum reservation count must be positive")
    return max(0, maximum - unfinished)


def plan_consecutive_first(
    *,
    target_date: date,
    preferences: Iterable[BlockPreference],
    available: Iterable[SlotKey],
    unfinished: int,
    maximum: int = 3,
    max_new: int = 1,
    fallback_to_single: bool = True,
) -> BookingPlan:
    """Select slots without exceeding either the account or job limit.

    Preferences are evaluated in ascending priority order. A complete block wins
    over a partial block. When fallback is enabled, available members of an
    incomplete block are considered in their declared order.
    """

    capacity = remaining_capacity(unfinished, maximum)
    budget = min(capacity, max(0, max_new))
    if budget == 0:
        return BookingPlan((), unfinished, capacity)

    available_set = set(available)
    selected: list[SlotKey] = []
    selected_set: set[SlotKey] = set()

    for preference in sorted(preferences, key=lambda item: item.priority):
        block = tuple(
            SlotKey(preference.venue, preference.sport, target_date, time)
            for time in preference.times
        )
        remaining = budget - len(selected)
        if remaining <= 0:
            break

        if len(block) <= remaining and all(slot in available_set for slot in block):
            for slot in block:
                if slot not in selected_set:
                    selected.append(slot)
                    selected_set.add(slot)
            continue

        if fallback_to_single:
            for slot in block:
                if len(selected) >= budget:
                    break
                if slot in available_set and slot not in selected_set:
                    selected.append(slot)
                    selected_set.add(slot)

    return BookingPlan(tuple(selected), unfinished, capacity)
