from datetime import date

import pytest

from fudan_booking.models import BlockPreference, SlotKey
from fudan_booking.policy import plan_consecutive_first, remaining_capacity

TARGET_DATE = date(2026, 9, 22)


def slot(venue: str, time: str) -> SlotKey:
    return SlotKey(venue, "羽毛球", TARGET_DATE, time)


def test_capacity_never_exceeds_three() -> None:
    assert remaining_capacity(0) == 3
    assert remaining_capacity(1) == 2
    assert remaining_capacity(2) == 1
    assert remaining_capacity(3) == 0
    assert remaining_capacity(5) == 0


def test_negative_unfinished_is_rejected() -> None:
    with pytest.raises(ValueError):
        remaining_capacity(-1)


def test_complete_consecutive_block_is_preferred() -> None:
    preferences = [
        BlockPreference("北区体育馆", "羽毛球", ("19:00-20:00", "20:00-21:00"), 0),
        BlockPreference("正大体育馆", "羽毛球", ("19:00-20:00",), 1),
    ]
    available = {
        slot("北区体育馆", "19:00-20:00"),
        slot("北区体育馆", "20:00-21:00"),
        slot("正大体育馆", "19:00-20:00"),
    }
    plan = plan_consecutive_first(
        target_date=TARGET_DATE,
        preferences=preferences,
        available=available,
        unfinished=1,
        max_new=2,
    )
    assert plan.selected == (
        slot("北区体育馆", "19:00-20:00"),
        slot("北区体育馆", "20:00-21:00"),
    )


def test_capacity_truncates_a_block() -> None:
    preference = BlockPreference(
        "北区体育馆", "羽毛球", ("19:00-20:00", "20:00-21:00"), 0
    )
    plan = plan_consecutive_first(
        target_date=TARGET_DATE,
        preferences=[preference],
        available={slot("北区体育馆", "19:00-20:00"), slot("北区体育馆", "20:00-21:00")},
        unfinished=2,
        max_new=2,
    )
    assert plan.selected == (slot("北区体育馆", "19:00-20:00"),)


def test_at_capacity_monitors_but_plans_no_booking() -> None:
    preference = BlockPreference("北区体育馆", "羽毛球", ("19:00-20:00",), 0)
    plan = plan_consecutive_first(
        target_date=TARGET_DATE,
        preferences=[preference],
        available={slot("北区体育馆", "19:00-20:00")},
        unfinished=3,
        max_new=1,
    )
    assert plan.selected == ()
    assert plan.capacity_before == 0
