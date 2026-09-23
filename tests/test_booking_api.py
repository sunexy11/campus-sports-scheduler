from datetime import date

import pytest
import requests

from fudan_booking.auth import UISCredentials
from fudan_booking.booking_api import BookingReadClient, normalize_time_range
from fudan_booking.errors import AntiBotChallenge, BookingError


def test_normalize_time_range_accepts_single_digit_hours() -> None:
    assert normalize_time_range("9:00-10:00") == "09:00-10:00"
    assert normalize_time_range("09:00-10:00") == "09:00-10:00"


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class BookingResponse(FakeResponse):
    status_code = 200
    url = "https://booking.fudan.edu.cn/reservation/site/resource/launch"

    def raise_for_status(self) -> None:
        return None


class FakeBookingBridge:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []
        self.get_calls = []

    def book_resource(self, **kwargs):
        self.calls.append(kwargs)
        return BookingResponse(self.payload)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return BookingResponse({"e": "OK", "d": {"mobile": "13800000000"}})


class TimedReadResponse(FakeResponse):
    def __init__(self, payload: dict) -> None:
        super().__init__(payload)
        self.status_code = 200
        self.url = "https://booking.fudan.edu.cn/reservation/api/user/unfinished"
        self.headers = {
            "X-Fudan-Request-Elapsed-Ms": "4210",
            "X-Fudan-First-Get-Elapsed-Ms": "300",
            "X-Fudan-Challenge-Elapsed-Ms": "3500",
            "X-Fudan-Challenge-Completed": "true",
            "X-Fudan-Challenge-Completion-Signal": "cookie",
        }


class TimedReadBridge:
    def get(self, url, **kwargs):
        return TimedReadResponse({"e": "OK", "d": {"data": []}})


class FakeSession:
    def __init__(self, responses: list[dict]) -> None:
        self.headers = {}
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return FakeResponse(next(self.responses))


def test_resource_and_schedule_queries_are_read_only() -> None:
    session = FakeSession(
        [
            {
                "e": "OK",
                "d": {
                    "data": [
                        {"base": {"id": 938, "name": "北区体育馆-羽毛球", "type": 6}}
                    ]
                },
            },
            {"e": "OK", "d": {"time": [], "resource": [], "data": {}}},
        ]
    )
    client = BookingReadClient(session)
    resources = client.list_resources()
    schedule = client.get_schedule(938, date(2026, 9, 22))

    assert resources[0].resource_id == 938
    assert resources[0].name == "北区体育馆-羽毛球"
    assert schedule == {"time": [], "resource": [], "data": {}}
    assert all("timeout" in kwargs for _, kwargs in session.requests)


def test_read_request_timing_records_challenge_without_url_query_or_secrets() -> None:
    session = FakeSession([])
    session._fudan_cas_bridge = TimedReadBridge()
    client = BookingReadClient(session)

    client._get("https://booking.fudan.edu.cn/reservation/api/user/unfinished?token=private")

    assert client.read_request_timings == [
        {
            "endpoint": "/reservation/api/user/unfinished",
            "status": 200,
            "request_elapsed_ms": 4210,
            "first_get_elapsed_ms": 300,
            "challenge_elapsed_ms": 3500,
            "challenge_completed": True,
            "challenge_completion_signal": "cookie",
            "challenge_attempted": True,
        }
    ]
    assert "private" not in repr(client.read_request_timings)


def test_unfinished_list_is_returned_without_cancellation_surface() -> None:
    session = FakeSession([{"e": "OK", "d": {"data": [{"id": 123}]}}])
    client = BookingReadClient(session)
    assert client.list_unfinished() == [{"id": 123}]
    assert not hasattr(client, "cancel")


def test_unfinished_list_excludes_cancelled_and_past_reservations() -> None:
    session = FakeSession(
        [
            {
                "e": "OK",
                "d": {
                    "data": [
                        {"id": 1, "is_cancel": 0, "detail": {"2099-01-01": []}},
                        {"id": 2, "status": 0, "detail": {"2099-01-01": []}},
                        {"id": 3, "status": 3, "detail": {"2020-01-01": []}},
                        {"id": 4, "status": 3, "is_cancel": 1, "detail": {"2099-01-01": []}},
                    ]
                },
            }
        ]
    )
    client = BookingReadClient(session)
    assert client.list_unfinished() == [
        {"id": 4, "status": 3, "is_cancel": 1, "detail": {"2099-01-01": []}}
    ]


def test_schedule_is_normalized_without_exposing_occupants() -> None:
    session = FakeSession(
        [
            {
                "e": "OK",
                "d": {
                    "time": [
                        {"id": 3800, "str_time": "19:00-20:00"},
                        {"id": 3801, "str_time": "20:00-21:00"},
                    ],
                    "resource": [
                        {"id": 939, "name": "1号场地"},
                        {"id": 940, "name": "2号场地"},
                    ],
                    "data": {
                        "2026-09-22": {
                            "939": {
                                "3800": {"status": 0, "total": 1, "num": "1", "user": "不应输出"},
                                "3801": {"status": 3, "total": 1, "num": "0", "user": "不应输出"},
                            },
                            "940": {
                                "3800": {"status": 3, "total": 1, "num": "0"},
                                "3801": {"status": 3, "total": 1, "num": "0"},
                            },
                        },
                    },
                },
            }
        ]
    )
    availability = BookingReadClient(session).get_availability(938, date(2026, 9, 22))

    assert availability.sub_resource_ids == (939, 940)
    assert availability.periods[0].available is True
    assert availability.periods[0].available_sub_resources == 1
    assert availability.periods[0].available_sub_resource_ids == (939,)
    assert availability.periods[1].available is False


def test_missing_slot_data_is_not_assumed_available() -> None:
    session = FakeSession(
        [
            {
                "e": "OK",
                "d": {
                    "time": [{"id": 3800, "str_time": "19:00-20:00"}],
                    "resource": [{"id": 939, "name": "1号场地"}],
                    "data": {"2026-09-22": {}},
                },
            }
        ]
    )
    availability = BookingReadClient(session).get_availability(938, date(2026, 9, 22))

    assert availability.periods[0].available is False


def test_status_zero_slot_with_capacity_is_available() -> None:
    session = FakeSession(
        [
            {
                "e": "OK",
                "d": {
                    "time": [{"id": 3800, "str_time": "19:00-20:00"}],
                    "resource": [{"id": 939, "name": "1号场地"}],
                    "data": {
                        "2026-09-22": {
                            "939": {"3800": {"status": 0, "total": 1, "num": "1"}},
                        }
                    },
                },
            }
        ]
    )

    availability = BookingReadClient(session).get_availability(938, date(2026, 9, 22))

    assert availability.periods[0].available is True


def test_api_http_error_does_not_expose_query_values() -> None:
    response = requests.Response()
    response.status_code = 403
    response.url = "https://booking.fudan.edu.cn/private?ticket=secret"
    client = BookingReadClient(FakeSession([]))

    with pytest.raises(BookingError) as error:
        client._json(response, "resource list")

    assert "resource list HTTP 403" in str(error.value)
    assert "ticket=secret" not in str(error.value)


def test_booking_412_reports_challenge_timing_without_cookie_values() -> None:
    response = requests.Response()
    response.status_code = 412
    response.url = "https://booking.fudan.edu.cn/reservation/site/resource/launch"
    response.headers.update(
        {
            "X-Fudan-First-Post-Elapsed-Ms": "312",
            "X-Fudan-Challenge-Elapsed-Ms": "3001",
            "X-Fudan-Challenge-Completion-Signal": "timeout",
            "X-Fudan-Retry-Post-Elapsed-Ms": "318",
            "X-Fudan-Retry-Challenge-Elapsed-Ms": "5002",
            "X-Fudan-Retry-Challenge-Completion-Signal": "cookie",
            "X-Fudan-Final-Post-Elapsed-Ms": "4910",
        }
    )
    client = BookingReadClient(FakeSession([]))

    with pytest.raises(AntiBotChallenge) as error:
        client._json(response, "booking submission")

    message = str(error.value)
    assert "first_post_elapsed_ms=312" in message
    assert "challenge_elapsed_ms=3001" in message
    assert "challenge_completion_signal=timeout" in message
    assert "retry_challenge_elapsed_ms=5002" in message
    assert "retry_challenge_completion_signal=cookie" in message
    assert "final_post_elapsed_ms=4910" in message


def test_read_412_reports_bounded_challenge_timing() -> None:
    response = requests.Response()
    response.status_code = 412
    response.url = (
        "https://booking.fudan.edu.cn/reservation/site/appointment/"
        "appointment-list?p=1"
    )
    response.headers.update(
        {
            "X-Fudan-First-Get-Elapsed-Ms": "216",
            "X-Fudan-Challenge-Elapsed-Ms": "2741",
            "X-Fudan-Challenge-Completion-Signal": "cookie",
            "X-Fudan-Retry-Get-Elapsed-Ms": "301",
            "X-Fudan-Retry-Challenge-Elapsed-Ms": "8004",
            "X-Fudan-Retry-Challenge-Completion-Signal": "timeout",
            "X-Fudan-Final-Get-Elapsed-Ms": "287",
        }
    )
    client = BookingReadClient(FakeSession([]))

    with pytest.raises(AntiBotChallenge) as error:
        client._json(response, "unfinished reservation list")

    message = str(error.value)
    assert "两轮 Cookie 挑战后仍未通过" in message
    assert "first_get_elapsed_ms=216" in message
    assert "challenge_elapsed_ms=2741" in message
    assert "challenge_completion_signal=cookie" in message
    assert "retry_challenge_elapsed_ms=8004" in message
    assert "retry_challenge_completion_signal=timeout" in message
    assert "final_get_elapsed_ms=287" in message
    assert "?p=1" not in message


def test_login_preflights_booking_page_before_cas(monkeypatch) -> None:
    events = []

    class PreflightSession:
        def __init__(self) -> None:
            self.headers = {}

        def get(self, url, **kwargs):
            events.append(("preflight", url))
            return FakeResponse({})

    session = PreflightSession()
    monkeypatch.setattr("fudan_booking.booking_api.build_session", lambda: session)

    def fake_login(credentials, service, session):
        events.append(("cas", service))
        assert credentials.username == "student"
        return session

    monkeypatch.setattr("fudan_booking.booking_api.login_to_service", fake_login)

    client = BookingReadClient.login(UISCredentials("student", "password"))

    assert client.session is session
    assert events[0][0] == "preflight"
    assert events[1][0] == "cas"
    assert "/reservation/fe/site/special/special" in events[0][1]


def test_submit_booking_uses_only_the_dedicated_bridge_operation() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "OK", "d": {"process_id": 123}})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939, 940),
        period_id=3800,
        target_date=date(2026, 9, 22),
        phone="13812345678",
        number=1,
    )

    assert result.ok is True
    assert result.process_id == 123
    assert bridge.calls == [
        {
            "group_id": 938,
            "sub_resource_ids": (939, 940),
            "period_id": 3800,
            "target_date": "2026-09-22",
            "phone": "13812345678",
            "number": 1,
        }
    ]


def test_submit_booking_classifies_normal_racing_rejections() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "ERROR", "m": "已有未结束的预约"})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939,),
        period_id=3800,
        target_date=date(2026, 9, 22),
        phone="13812345678",
    )

    assert result == result.__class__(False, "existing_reservation")


def test_submit_booking_classifies_slot_race_without_raising() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "ERROR", "m": "该场地已被预约"})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939,),
        period_id=3800,
        target_date=date(2026, 9, 22),
    )

    assert result == result.__class__(False, "slot_unavailable")


def test_submit_booking_classifies_overlapping_existing_slot_without_raising() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "ERROR", "m": "预约时间不可重叠"})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939,),
        period_id=3800,
        target_date=date(2026, 9, 22),
    )

    assert result == result.__class__(False, "overlap_with_existing")


def test_submit_booking_classifies_generic_duplicate_time_rejection() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "ERROR", "m": "该时间段重复预约"})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939,),
        period_id=3800,
        target_date=date(2026, 9, 22),
    )

    assert result == result.__class__(False, "overlap_with_existing")


def test_submit_booking_uses_authenticated_default_contact() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "OK", "d": {}})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939,),
        period_id=3800,
        target_date=date(2026, 9, 22),
    )

    assert result.ok is True
    assert bridge.calls[0]["phone"] == "13800000000"


def test_authenticated_default_contact_is_cached_for_multiple_submissions() -> None:
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "OK", "d": {"process_id": 123}})
    session._fudan_cas_bridge = bridge
    client = BookingReadClient(session)

    for period_id in (3800, 3801):
        result = client.submit_booking(
            group_id=938,
            sub_resource_ids=(939,),
            period_id=period_id,
            target_date=date(2026, 9, 22),
        )
        assert result.ok is True

    assert len(bridge.calls) == 2
    assert len(bridge.get_calls) == 1


def test_configured_mobile_avoids_profile_request(monkeypatch) -> None:
    monkeypatch.setenv("FUDAN_MOBILE", "13912345678")
    session = FakeSession([])
    bridge = FakeBookingBridge({"e": "OK", "d": {}})
    session._fudan_cas_bridge = bridge

    result = BookingReadClient(session).submit_booking(
        group_id=938,
        sub_resource_ids=(939,),
        period_id=3800,
        target_date=date(2026, 9, 22),
    )

    assert result.ok is True
    assert bridge.calls[0]["phone"] == "13912345678"
    assert bridge.get_calls == []
