from datetime import date

from fudan_booking.booking_api import PeriodAvailability, ResourceAvailability, ResourceSummary
from fudan_booking.monitor import _target_dates, monitor_once


class FakeClient:
    def list_resources(self):
        return [ResourceSummary(938, "北区体育馆-羽毛球", 6)]

    def get_availability(self, resource_id, target_date):
        assert resource_id == 938
        return ResourceAvailability(
            resource_id,
            target_date,
            (939,),
            (PeriodAvailability(3800, "19:00-20:00", 1, 4),),
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
    assert len(notifier.messages) == 1


def test_monitor_once_does_not_send_when_no_notifier():
    assert monitor_once(
        FakeClient(),
        {"monitor": {"jobs": []}},
        today=date(2026, 9, 20),
    ) == []
