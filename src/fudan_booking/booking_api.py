from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from .auth import UISCredentials, build_session, login_to_service
from .errors import AntiBotChallenge, AuthenticationExpired, BookingError

BOOKING_BASE = "https://booking.fudan.edu.cn"
BOOKING_CAS_SERVICE = f"{BOOKING_BASE}/reservation/api/login/cas"
SPORTS_TOPIC_ID = 48
SPORTS_PAGE = f"{BOOKING_BASE}/reservation/fe/site/special/special?id={SPORTS_TOPIC_ID}"


@dataclass(frozen=True, slots=True)
class ResourceSummary:
    resource_id: int
    name: str
    resource_type: int | None


@dataclass(frozen=True, slots=True)
class PeriodAvailability:
    period_id: int
    time: str
    available_sub_resources: int
    total_sub_resources: int

    @property
    def available(self) -> bool:
        return self.available_sub_resources > 0


@dataclass(frozen=True, slots=True)
class ResourceAvailability:
    resource_id: int
    target_date: date
    sub_resource_ids: tuple[int, ...]
    periods: tuple[PeriodAvailability, ...]


def _is_unoccupied(value: object) -> bool:
    """Accept the boolean and numeric encodings used by the booking API."""

    if value is False or value == 0:
        return True
    return isinstance(value, str) and value.strip().lower() in {"0", "false"}


@dataclass(frozen=True, slots=True)
class BookingSubmission:
    """Result of one live booking submission.

    The API may reject a request because the service window is closed or the
    account already has an unfinished reservation. Those are normal outcomes
    for a racing scheduler and are represented without exposing the response
    body in logs.
    """

    ok: bool
    reason: str
    process_id: int | None = None


class BookingReadClient:
    """Read-only booking API client used before live-booking approval."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.session.headers["Referer"] = SPORTS_PAGE

    def _get(self, url: str, **kwargs) -> requests.Response:
        """Use the persistent no-browser bridge when CAS created one."""

        bridge = getattr(self.session, "_fudan_cas_bridge", None)
        if bridge is None:
            return self.session.get(url, **kwargs)
        params = kwargs.pop("params", None)
        headers = dict(self.session.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        prepared = requests.Request("GET", url, params=params).prepare()
        return bridge.get(prepared.url, headers=headers)

    def submit_booking(
        self,
        *,
        group_id: int,
        sub_resource_ids: tuple[int, ...],
        period_id: int,
        target_date: date,
        phone: str = "",
        number: int = 1,
    ) -> BookingSubmission:
        """Submit one sports booking; an empty phone uses the server default.

        Cancellation is intentionally not supported. The optional phone is only
        an override; normal calls send an empty contact collection so the site
        can use the contact value already associated with the account.
        """

        if group_id <= 0 or period_id <= 0:
            raise ValueError("group_id and period_id must be positive")
        if not sub_resource_ids or any(item <= 0 for item in sub_resource_ids):
            raise ValueError("sub_resource_ids must contain positive IDs")
        if not 1 <= number <= 100:
            raise ValueError("number must be between 1 and 100")

        payload = {
            "data": json.dumps(
                [
                    {
                        "resource_ids": list(sub_resource_ids),
                        "group_id": group_id,
                        "period": [
                            {"date": target_date.isoformat(), "period_id": period_id}
                        ],
                        "number": number,
                    }
                ],
                ensure_ascii=False,
            ),
            "data_colle": json.dumps(
                [
                    {
                        "name": "手机号",
                        "value": phone,
                        "verify": None,
                        "type": "mobile",
                    }
                ],
                ensure_ascii=False,
            )
            if phone
            else "[]",
            "collective": "0",
            "captcha": json.dumps({"token": "", "pointJson": ""}),
        }
        endpoint = f"{BOOKING_BASE}/reservation/site/resource/launch"
        bridge = getattr(self.session, "_fudan_cas_bridge", None)
        if bridge is not None:
            response = bridge.book_resource(
                group_id=group_id,
                sub_resource_ids=sub_resource_ids,
                period_id=period_id,
                target_date=target_date.isoformat(),
                phone=phone,
                number=number,
            )
        else:
            response = self.session.post(
                endpoint,
                data=payload,
                headers={"Referer": SPORTS_PAGE},
                timeout=20,
            )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            self._json(response, "booking submission")
            raise BookingError("booking submission failed") from exc
        try:
            body = response.json()
        except requests.JSONDecodeError as exc:
            raise BookingError("booking submission returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise BookingError("booking submission returned invalid data")
        if body.get("e") != "OK":
            message = str(body.get("m") or "")
            if "服务时间" in message:
                return BookingSubmission(False, "not_in_service_time")
            if "未结束的预约" in message:
                return BookingSubmission(False, "existing_reservation")
            raise BookingError("booking submission was rejected")
        data = body.get("d")
        if not isinstance(data, dict):
            raise BookingError("booking submission returned invalid result")
        process_id = data.get("process_id")
        return BookingSubmission(
            True,
            "submitted",
            int(process_id) if process_id is not None else None,
        )

    @classmethod
    def login(cls, credentials: UISCredentials) -> BookingReadClient:
        session = build_session()
        preflight = session.get(SPORTS_PAGE, allow_redirects=True, timeout=20)
        try:
            preflight.raise_for_status()
        except requests.HTTPError as exc:
            raise BookingError(
                f"booking site preflight HTTP {preflight.status_code}"
            ) from exc
        session = login_to_service(credentials, BOOKING_CAS_SERVICE, session=session)
        return cls(session)

    def _json(self, response: requests.Response, operation: str) -> dict:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            parsed = urlsplit(response.url)
            endpoint = f"{parsed.hostname or 'unknown-host'}{parsed.path}"
            if response.status_code == 412 and parsed.hostname == "booking.fudan.edu.cn":
                raise AntiBotChallenge(
                    f"{operation} HTTP 412：booking.fudan.edu.cn 启用了瑞数反爬校验；"
                    "当前纯 requests 客户端无法完成 JavaScript 校验"
                ) from exc
            raise BookingError(
                f"{operation} HTTP {response.status_code} ({endpoint})"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise BookingError(f"{operation} returned an unexpected payload")
        if payload.get("e") != "OK":
            message = str(payload.get("m") or "")
            if "无权" in message or "登录" in message:
                raise AuthenticationExpired("booking session is no longer valid")
            raise BookingError(f"{operation} failed")
        data = payload.get("d")
        if not isinstance(data, dict):
            raise BookingError(f"{operation} returned invalid data")
        return data

    def list_resources(self, topic_id: int = SPORTS_TOPIC_ID) -> list[ResourceSummary]:
        response = self._get(
            f"{BOOKING_BASE}/reservation/api/topic/resource-list",
            params={"id": topic_id, "pageSize": 200},
            timeout=20,
        )
        data = self._json(response, "resource list")
        resources: list[ResourceSummary] = []
        for item in data.get("data") or []:
            base = item.get("base") or {}
            if base.get("id") is None or not base.get("name"):
                continue
            resources.append(
                ResourceSummary(
                    resource_id=int(base["id"]),
                    name=str(base["name"]),
                    resource_type=int(base["type"]) if base.get("type") is not None else None,
                )
            )
        return resources

    def get_schedule(self, resource_id: int, target_date: date) -> dict:
        """Read the legacy large-screen shape kept for compatibility/tests.

        The booking page no longer uses this endpoint for its date calendar;
        callers that need availability should use :meth:`get_availability`.
        """

        response = self._get(
            f"{BOOKING_BASE}/reservation/api/resource/large-screen",
            params={"id": resource_id, "date": target_date.isoformat()},
            timeout=20,
        )
        return self._json(response, "schedule")

    def get_calendar(self, resource_id: int, target_date: date) -> dict:
        """Read the same date calendar endpoint used by the web page.

        The server accepts a JSON date range rather than a single ``date``
        query parameter.  Keeping the range to one day makes the returned
        payload unambiguous and avoids client-side date/time filtering.
        """

        response = self._get(
            f"{BOOKING_BASE}/reservation/site/resource/calendar",
            params={
                "id": resource_id,
                "collective": 0,
                "date": json.dumps(
                    {
                        "start_date": target_date.isoformat(),
                        "end_date": target_date.isoformat(),
                    },
                    separators=(",", ":"),
                ),
            },
            timeout=20,
        )
        return self._json(response, "resource calendar")

    def get_availability(
        self,
        resource_id: int,
        target_date: date,
    ) -> ResourceAvailability:
        """Normalize the web calendar without exposing occupant details.

        ``status`` is authoritative: the site itself marks expired, closed,
        disabled and full periods.  We intentionally do not compare the
        current clock with a period here.
        """

        schedule = self.get_calendar(resource_id, target_date)
        raw_times = schedule.get("time") or []
        raw_resources = schedule.get("resource") or []
        raw_data = schedule.get("data") or {}
        if not isinstance(raw_times, list):
            raise BookingError("schedule returned invalid time slots")
        if not isinstance(raw_resources, list):
            raise BookingError("schedule returned invalid sub-resources")
        if not isinstance(raw_data, dict):
            raise BookingError("schedule returned invalid availability data")

        sub_resource_ids = tuple(
            int(item["id"])
            for item in raw_resources
            if isinstance(item, dict) and item.get("id") is not None
        )
        date_data = raw_data.get(target_date.isoformat())
        if not isinstance(date_data, dict):
            raise BookingError("resource calendar did not return the requested date")

        periods: list[PeriodAvailability] = []
        for item in raw_times:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            period_id = int(item["id"])
            label = str(item.get("str_time") or period_id)
            available_count = 0
            for sub_resource_id in sub_resource_ids:
                slots = date_data.get(
                    str(sub_resource_id), date_data.get(sub_resource_id, {})
                )
                if not isinstance(slots, dict):
                    continue
                slot = slots.get(str(period_id), slots.get(period_id))
                if not isinstance(slot, dict):
                    continue
                # The booking page uses status=0 for a selectable slot.  The
                # numeric ``num`` is an additional guard for unusual payloads
                # where a selectable-looking slot has no remaining capacity.
                try:
                    remaining = int(slot.get("num", 0))
                except (TypeError, ValueError):
                    remaining = 0
                if slot.get("status") == 0 and remaining > 0:
                    available_count += 1
            periods.append(
                PeriodAvailability(
                    period_id=period_id,
                    time=label,
                    available_sub_resources=available_count,
                    total_sub_resources=len(sub_resource_ids),
                )
            )

        return ResourceAvailability(
            resource_id=resource_id,
            target_date=target_date,
            sub_resource_ids=sub_resource_ids,
            periods=tuple(periods),
        )
    def list_unfinished(self) -> list[dict]:
        response = self._get(
            f"{BOOKING_BASE}/reservation/site/appointment/appointment-list",
            params={"p": 1, "pageSize": 100, "type": 0},
            timeout=20,
        )
        data = self._json(response, "unfinished reservation list")
        items = data.get("data") or []
        if not isinstance(items, list):
            raise BookingError("unfinished reservation list returned invalid items")
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        unfinished: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("is_cancel")) == "1" or str(item.get("status")) == "0":
                continue
            detail = item.get("detail")
            if isinstance(detail, dict):
                dates = []
                for value in detail:
                    try:
                        dates.append(date.fromisoformat(str(value)))
                    except ValueError:
                        continue
                if dates and max(dates) < today:
                    continue
            unfinished.append(item)
        return unfinished
