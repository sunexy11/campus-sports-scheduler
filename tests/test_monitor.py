from datetime import date

from fudan_booking.booking_api import (
    BookingSubmission,
    PeriodAvailability,
    ResourceAvailability,
    ResourceSummary,
)
from fudan_booking.monitor import _target_dates, monitor_and_book_once, monitor_once


class FakeClient:
    def list_resources(self):
        return [ResourceSummary(938, "北区体育馆-羽毛球", 6)]

    def get_availability(self, resource_id, target_date):
        assert resource_id == 938
        return ResourceAvailability(
            resource_id,
            target_date,
            (939,),
            (PeriodAvailability(3800, "19:00-20:00", 1, 4, (939,)),),
        )


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, subject, text):
        self.messages.append((subject, text))


def test_next_three_days_means_today_tomorrow_and_day_after_tomorrow():
    assert _target_dates("next_3_days", date(2026, 9, 20)) == [
        date(2026, 9, 20),
        date(2026, 9, 21),
        date(2026, 9, 22),
    ]


def test_monitor_date_offsets_are_relative_and_empty_list_is_allowed():
    assert _target_dates([0, 1, 2], date(2026, 9, 20)) == [
        date(2026, 9, 20),
        date(2026, 9, 21),
        date(2026, 9, 22),
    ]
    assert _target_dates([1, 2, 1], date(2026, 9, 20)) == [
        date(2026, 9, 21),
        date(2026, 9, 22),
    ]
    assert _target_dates([], date(2026, 9, 20)) == []


def test_monitor_once_matches_venue_sport_and_notifies_once():
    notifier = FakeNotifier()
    findings = monitor_once(
        FakeClient(),
        {
            "monitor": {
                "jobs": [
                    {
                        "name": "北区晚间羽毛球",
                        "venue": "北区体育馆",
                        "sport": "羽毛球",
                        "dates": ["2026-09-22"],
                        "times": ["19:00-20:00"],
                    }
                ]
            }
        },
        notifier,
        today=date(2026, 9, 20),
    )

    assert findings[0]["resource_id"] == 938
    assert findings[0]["date"] == "2026-09-22"
    assert findings[0]["available_sub_resources"] == 1
    assert findings[0]["occupied_sub_resources"] == 3
    assert findings[0]["total_sub_resources"] == 4
    assert len(notifier.messages) == 1


def test_monitor_once_does_not_send_when_no_notifier():
    assert monitor_once(
        FakeClient(),
        {"monitor": {"jobs": []}},
        today=date(2026, 9, 20),
    ) == []


def test_monitor_matches_non_zero_padded_config_time():
    class MorningClient(FakeClient):
        def get_availability(self, resource_id, target_date):
            return ResourceAvailability(
                resource_id,
                target_date,
                (939,),
                (PeriodAvailability(3800, "09:00-10:00", 1, 1, (939,)),),
            )

    findings = monitor_once(
        MorningClient(),
        {
            "monitor": {
                "jobs": [
                    {
                        "venue": "北区体育馆",
                        "sport": "羽毛球",
                        "dates": ["2026-09-22"],
                        "times": ["9:00-10:00"],
                    }
                ]
            }
        },
        today=date(2026, 9, 20),
    )

    assert findings[0]["time"] == "09:00-10:00"


class RacingClient(FakeClient):
    def __init__(self):
        self.submit_calls = []
        self.unfinished_calls = 0

    def list_unfinished(self):
        self.unfinished_calls += 1
        return []

    def submit_booking(self, **kwargs):
        self.submit_calls.append(kwargs)
        # Simulate another user taking the only free sub-resource after the
        # scan. The runner must report and finish normally.
        return BookingSubmission(False, "slot_unavailable")


def test_monitor_booking_race_is_nonfatal_and_does_not_repeat_same_slot():
    client = RacingClient()
    result = monitor_and_book_once(
        client,
        {
            "monitor": {
                "jobs": [
                    {
                        "name": "北区晚间羽毛球",
                        "venue": "北区体育馆",
                        "sport": "羽毛球",
                        "dates": ["2026-09-22"],
                        "times": ["19:00-20:00"],
                        "mode": "auto_book_if_capacity",
                        "max_new_reservations": 1,
                    }
                ]
            }
        },
        today=date(2026, 9, 20),
        allow_booking=True,
    )

    assert result["booking_results"][0]["reason"] == "slot_unavailable"
    assert result["booking_results"][0]["ok"] is False
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0]["group_id"] == 938
    assert client.submit_calls[0]["sub_resource_ids"] == (939,)


class OverlapThenSuccessClient(FakeClient):
    def __init__(self):
        self.submit_calls = []
        self.results = iter(
            [BookingSubmission(False, "overlap_with_existing"), BookingSubmission(True, "submitted", 123)]
        )

    def get_availability(self, resource_id, target_date):
        return ResourceAvailability(
            resource_id,
            target_date,
            (939, 940),
            (
                PeriodAvailability(3800, "19:00-20:00", 1, 2, (939,)),
                PeriodAvailability(3801, "20:00-21:00", 1, 2, (940,)),
            ),
        )

    def list_unfinished(self):
        return []

    def submit_booking(self, **kwargs):
        self.submit_calls.append(kwargs)
        return next(self.results)


def test_monitor_skips_overlap_and_continues_with_other_slot():
    client = OverlapThenSuccessClient()
    result = monitor_and_book_once(
        client,
        {
            "monitor": {
                "jobs": [
                    {
                        "venue": "北区体育馆",
                        "sport": "羽毛球",
                        "dates": ["2026-09-22"],
                        "times": ["19:00-20:00", "20:00-21:00"],
                        "mode": "auto_book_if_capacity",
                        "max_new_reservations": 2,
                    }
                ]
            }
        },
        today=date(2026, 9, 20),
        allow_booking=True,
    )

    assert [item["reason"] for item in result["booking_results"]] == [
        "overlap_with_existing",
        "submitted",
    ]
    assert len(client.submit_calls) == 2
