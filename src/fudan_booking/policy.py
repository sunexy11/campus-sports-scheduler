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

    Preferences are evaluated in ascending priority order. Complete blocks are
    considered before every partial block. When fallback is enabled, available
    members of incomplete blocks are considered only after the complete-block
    pass, again in declared priority order.
    """

    capacity = remaining_capacity(unfinished, maximum)
    budget = min(capacity, max(0, max_new))
    if budget == 0:
        return BookingPlan((), unfinished, capacity)

    available_set = set(available)
    selected: list[SlotKey] = []
    selected_set: set[SlotKey] = set()

    ordered_preferences = sorted(preferences, key=lambda item: item.priority)

    # First select complete blocks. This keeps a partial high-priority block
    # from consuming the budget before a lower-priority complete block can be
    # considered.
    for preference in ordered_preferences:
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

    if fallback_to_single and len(selected) < budget:
        for preference in ordered_preferences:
            block = tuple(
                SlotKey(preference.venue, preference.sport, target_date, time)
                for time in preference.times
            )
            for slot in block:
                if len(selected) >= budget:
                    break
                if slot in available_set and slot not in selected_set:
                    selected.append(slot)
                    selected_set.add(slot)
            if len(selected) >= budget:
                break

    return BookingPlan(tuple(selected), unfinished, capacity)
