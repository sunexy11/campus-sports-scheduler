from datetime import date

import requests

from fudan_booking.booking_api import (
    BookingSubmission,
    PeriodAvailability,
    ResourceAvailability,
    ResourceSummary,
)
from fudan_booking.booking_runner import (
    prewarm_scheduled_context,
    scheduled_book_once,
    scheduled_book_with_retries,
)
from fudan_booking.errors import BookingError


class FakeScheduledClient:
    def __init__(self, result: BookingSubmission | None = None):
        self.result = result or BookingSubmission(False, "slot_unavailable")
        self.calls = []
        self.availability_calls = []

    def list_resources(self):
        return [ResourceSummary(938, "北区体育馆-羽毛球", 6)]

    def get_availability(self, resource_id, target_date):
        self.availability_calls.append((resource_id, target_date))
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


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def test_scheduled_book_defaults_to_dry_run():
    client = FakeScheduledClient()
    result = scheduled_book_once(
        client, _config(), today=date(2026, 9, 20), allow_booking=False
    )

    assert result["jobs"][0]["mode"] == "dry_run"
    assert result["jobs"][0]["planned"] == ["19:00-20:00", "20:00-21:00"]
    assert client.calls == []
    assert client.availability_calls == [(938, date(2026, 9, 22))]


def test_scheduled_booking_continues_after_racing_rejection():
    client = FakeScheduledClient()
    result = scheduled_book_once(
        client, _config(), today=date(2026, 9, 20), allow_booking=True
    )

    assert len(result["jobs"][0]["results"]) == 2
    assert all(item["reason"] == "slot_unavailable" for item in result["jobs"][0]["results"])


def test_scheduled_booking_clamps_to_existing_account_capacity():
    class ExistingClient(FakeScheduledClient):
        def list_unfinished(self):
            return [{}, {}]

    client = ExistingClient()
    result = scheduled_book_once(
        client, _config(), today=date(2026, 9, 20), allow_booking=False
    )

    assert result["unfinished_reservation_count"] == 2
    assert result["remaining_capacity"] == 1
    assert result["jobs"][0]["planned"] == ["19:00-20:00"]


def test_scheduled_booking_skips_site_queries_when_job_budget_is_zero():
    class NoQueryClient(FakeScheduledClient):
        def list_unfinished(self):
            raise AssertionError("should not read reservations")

        def list_resources(self):
            raise AssertionError("should not read resources")

    config = _config()
    config["scheduled_jobs"][0]["max_new_reservations"] = 0
    result = scheduled_book_once(
        NoQueryClient(), config, today=date(2026, 9, 20), allow_booking=True
    )

    assert result["jobs"][0]["stop_reason"] == "max_new_reservations_zero"


def test_scheduled_context_prewarm_deduplicates_resource_and_date():
    client = FakeScheduledClient()
    prewarm_scheduled_context(
        client,
        _config(),
        today=date(2026, 9, 20),
        resources=client.list_resources(),
    )

    # One configured venue/date is warmed once even though the preference has
    # multiple blocks; the actual booking pass still refreshes the calendar.
    assert client.calls == []


def test_opening_retry_survives_temporary_read_errors_and_reaches_job_limit():
    class FlakyClient(FakeScheduledClient):
        def __init__(self):
            super().__init__(BookingSubmission(True, "submitted", 123))
            self.failures_left = 2

        def list_unfinished(self):
            if self.failures_left:
                self.failures_left -= 1
                raise BookingError("temporary read failure")
            return []

    config = _config()
    config["scheduled_jobs"][0]["max_new_reservations"] = 1
    client = FlakyClient()
    clock = FakeClock()

    result = scheduled_book_with_retries(
        client,
        config,
        today=date(2026, 9, 20),
        allow_booking=True,
        prepared_resources=client.list_resources(),
        retry_window_seconds=180,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["stop_reason"] == "max_new_reservations_reached"
    assert result["transient_error_count"] == 2
    assert result["rounds"] == 3
    assert len(client.calls) == 1


def test_opening_retry_keeps_polling_until_delayed_slot_appears():
    class DelayedAvailabilityClient(FakeScheduledClient):
        def __init__(self):
            super().__init__(BookingSubmission(True, "submitted", 123))
            self.calendar_reads = 0

        def get_availability(self, resource_id, target_date):
            self.calendar_reads += 1
            if self.calendar_reads < 3:
                return ResourceAvailability(
                    resource_id,
                    target_date,
                    (939, 940),
                    (
                        PeriodAvailability(3800, "19:00-20:00", 0, 2, ()),
                        PeriodAvailability(3801, "20:00-21:00", 0, 2, ()),
                    ),
                )
            return super().get_availability(resource_id, target_date)

    config = _config()
    config["scheduled_jobs"][0]["max_new_reservations"] = 1
    client = DelayedAvailabilityClient()
    clock = FakeClock()

    result = scheduled_book_with_retries(
        client,
        config,
        today=date(2026, 9, 20),
        allow_booking=True,
        prepared_resources=client.list_resources(),
        retry_window_seconds=180,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["stop_reason"] == "max_new_reservations_reached"
    assert client.calendar_reads == 3
    assert len(client.calls) == 1


def test_opening_retry_reconciles_ambiguous_submit_before_retrying():
    class AcceptedButDisconnectedClient(FakeScheduledClient):
        def __init__(self):
            super().__init__()
            self.unfinished = 0

        def list_unfinished(self):
            return [{} for _ in range(self.unfinished)]

        def submit_booking(self, **kwargs):
            self.calls.append(kwargs)
            self.unfinished = 1
            raise requests.ConnectionError("response was lost")

    config = _config()
    config["scheduled_jobs"][0]["max_new_reservations"] = 1
    client = AcceptedButDisconnectedClient()
    clock = FakeClock()

    result = scheduled_book_with_retries(
        client,
        config,
        today=date(2026, 9, 20),
        allow_booking=True,
        prepared_resources=client.list_resources(),
        retry_window_seconds=180,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["stop_reason"] == "max_new_reservations_reached"
    assert len(client.calls) == 1
    assert result["jobs"][0]["results"][0]["ok"] is True
    assert result["jobs"][0]["results"][0]["reason"] == "submitted_unconfirmed"


def test_opening_retry_stops_immediately_when_account_is_full():
    class FullClient(FakeScheduledClient):
        def list_unfinished(self):
            return [{}, {}, {}]

    client = FullClient()
    clock = FakeClock()
    result = scheduled_book_with_retries(
        client,
        _config(),
        today=date(2026, 9, 20),
        allow_booking=True,
        prepared_resources=client.list_resources(),
        retry_window_seconds=180,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["stop_reason"] == "capacity_reached"
    assert client.calls == []
    assert clock.sleeps == []


def test_opening_retry_uses_a_relative_window():
    class NoAvailabilityClient(FakeScheduledClient):
        def get_availability(self, resource_id, target_date):
            return ResourceAvailability(
                resource_id,
                target_date,
                (939,),
                (PeriodAvailability(3800, "19:00-20:00", 0, 1, ()),),
            )

    config = _config()
    config["scheduled_jobs"][0]["max_new_reservations"] = 1
    client = NoAvailabilityClient()
    clock = FakeClock()
    clock.value = 100.0

    result = scheduled_book_with_retries(
        client,
        config,
        today=date(2026, 9, 20),
        allow_booking=True,
        prepared_resources=client.list_resources(),
        retry_window_seconds=3,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["stop_reason"] == "retry_window_elapsed"
    assert clock.value == 103.0
    assert client.calls == []
