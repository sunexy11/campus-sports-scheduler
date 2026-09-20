from datetime import date

from fudan_booking.booking_api import (
    BookingSubmission,
    PeriodAvailability,
    ResourceAvailability,
    ResourceSummary,
)
from fudan_booking.booking_runner import scheduled_book_once


class FakeScheduledClient:
    def __init__(self, result: BookingSubmission | None = None):
        self.result = result or BookingSubmission(False, "slot_unavailable")
        self.calls = []

    def list_resources(self):
        return [ResourceSummary(938, "北区体育馆-羽毛球", 6)]

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
        self.calls.append(kwargs)
        return self.result


def _config():
    return {
        "scheduled_jobs": [
            {
                "enabled": True,
                "name": "后天晚间羽毛球",
                "date_offset": 2,
                "max_new_reservations": 2,
                "fallback_to_single_slot": True,
                "preferences": [
                    {
                        "venue": "北区体育馆",
                        "sport": "羽毛球",
                        "blocks": [["19:00-20:00", "20:00-21:00"]],
                    }
                ],
            }
        ]
    }


def test_scheduled_book_defaults_to_dry_run():
    client = FakeScheduledClient()
    result = scheduled_book_once(
        client, _config(), today=date(2026, 9, 20), allow_booking=False
    )

    assert result["jobs"][0]["mode"] == "dry_run"
    assert result["jobs"][0]["planned"] == ["19:00-20:00", "20:00-21:00"]
    assert client.calls == []


def test_scheduled_booking_continues_after_racing_rejection():
    client = FakeScheduledClient()
    result = scheduled_book_once(
        client, _config(), today=date(2026, 9, 20), allow_booking=True
    )

    assert len(result["jobs"][0]["results"]) == 2
    assert all(item["reason"] == "slot_unavailable" for item in result["jobs"][0]["results"])
