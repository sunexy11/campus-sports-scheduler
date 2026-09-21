from __future__ import annotations

import json
import os
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
DETAIL_MOBILE_ENDPOINT = f"{BOOKING_BASE}/reservation/site/user/detail-mobile"


def normalize_time_range(value: str) -> str:
    """Normalize ``H:MM-H:MM`` and ``HH:MM-HH:MM`` to the site format."""

    try:
        start, end = value.strip().split("-", 1)
        start_hour, start_minute = (int(part) for part in start.split(":", 1))
        end_hour, end_minute = (int(part) for part in end.split(":", 1))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid time range: {value!r}") from exc
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        raise ValueError(f"invalid time range: {value!r}")
    if not (0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        raise ValueError(f"invalid time range: {value!r}")
    return f"{start_hour:02d}:{start_minute:02d}-{end_hour:02d}:{end_minute:02d}"


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
    available_sub_resource_ids: tuple[int, ...] = ()

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
    submit_elapsed_ms: int | None = None
    first_post_elapsed_ms: int | None = None
    retry_post_elapsed_ms: int | None = None
    final_post_elapsed_ms: int | None = None
    challenge_elapsed_ms: int | None = None
    challenge_completed: bool | None = None
    challenge_completion_signal: str | None = None
    retry_challenge_elapsed_ms: int | None = None
    retry_challenge_completed: bool | None = None
    retry_challenge_completion_signal: str | None = None


def _booking_rejection_reason(message: str) -> str | None:
    """Classify expected, non-fatal responses from a racing submit.

    The exact Chinese wording has changed between deployments, so matching is
    deliberately based on stable concepts.  Authentication, anti-bot and
    server errors remain exceptions and therefore still fail the run.
    """

    if not message:
        return None
    if "未结束的预约" in message or "已有未结束" in message:
        return "existing_reservation"
    if any(
        token in message
        for token in (
            "预约时间不可重叠",
            "预约时间不能重叠",
            "时间不可重叠",
            "时间不能重叠",
            "预约时间重叠",
            "时间段重叠",
            "不能重复预约",
            "已有相同时间段预约",
            "预约重复",
            "同一时间段",
            "同一时段",
            "同一时间",
            "重叠",
            "重复",
        )
    ):
        return "overlap_with_existing"
    if "服务时间" in message:
        return "not_in_service_time"
    if any(
        token in message
        for token in (
            "已被预约",
            "已被占用",
            "无空闲",
            "没有空余",
            "约满",
            "已满",
            "已过期",
            "不可预约",
            "预约冲突",
            "资源冲突",
            "当前存在未提交",
        )
    ):
        return "slot_unavailable"
    return None


class BookingReadClient:
    """Booking API client with read methods and one fixed submit surface."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.session.headers["Referer"] = SPORTS_PAGE
        self._cached_mobile: str | None = None
        # The authenticated profile is the fallback.  An explicitly supplied
        # secret avoids an extra profile GET after the opening-time wait.
        self._configured_mobile = os.getenv("FUDAN_MOBILE", "").strip()

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
        """Submit one sports booking; an empty phone follows the web form.

        Cancellation is intentionally not supported. The optional phone is only
        an override; bridge-backed calls read the authenticated account mobile,
        matching the contact collection populated by the web form.
        """

        if group_id <= 0 or period_id <= 0:
            raise ValueError("group_id and period_id must be positive")
        if not sub_resource_ids or any(item <= 0 for item in sub_resource_ids):
            raise ValueError("sub_resource_ids must contain positive IDs")
        if not 1 <= number <= 100:
            raise ValueError("number must be between 1 and 100")

        bridge = getattr(self.session, "_fudan_cas_bridge", None)
        effective_phone = phone
        if not effective_phone and self._configured_mobile:
            effective_phone = self._configured_mobile
            self._cached_mobile = effective_phone
        elif not effective_phone and bridge is not None:
            # 网页预约表单会自动填入账号手机号；部分资源将该字段标记为必填。
            # 只在真实桥接提交前读取，不把手机号写入配置或日志。
            effective_phone = self.default_mobile()

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
                        "value": effective_phone,
                        "verify": None,
                        "type": "mobile",
                    }
                ],
                ensure_ascii=False,
            )
            if effective_phone
            else "[]",
            "code": "",
            "number": number,
            "collective": "0",
            "captcha": json.dumps({"token": "", "pointJson": ""}),
        }
        endpoint = f"{BOOKING_BASE}/reservation/site/resource/launch"
        if bridge is not None:
            response = bridge.book_resource(
                group_id=group_id,
                sub_resource_ids=sub_resource_ids,
                period_id=period_id,
                target_date=target_date.isoformat(),
                phone=effective_phone,
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
            # A 409/422 is the normal optimistic-concurrency outcome: the
            # slot disappeared between the calendar read and the submit.
            if response.status_code in {409, 422}:
                return BookingSubmission(
                    False,
                    "slot_unavailable",
                    **self._submission_timing(response),
                )
            self._json(response, "booking submission")
            raise BookingError("booking submission failed") from exc
        try:
            body = response.json()
        except requests.JSONDecodeError as exc:
            raise BookingError("booking submission returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise BookingError("booking submission returned invalid data")
        if body.get("e") != "OK":
            message = str(body.get("m") or "").strip()
            reason = _booking_rejection_reason(message)
            if reason is not None:
                return BookingSubmission(
                    False,
                    reason,
                    **self._submission_timing(response),
                )
            # Keep an unknown business rejection diagnosable without dumping
            # the complete response (which may contain unrelated fields).
            detail = " ".join(message.split())[:160]
            suffix = f": {detail}" if detail else ""
            raise BookingError(f"booking submission was rejected{suffix}")
        data = body.get("d")
        if not isinstance(data, dict):
            raise BookingError("booking submission returned invalid result")
        process_id = data.get("process_id")
        return BookingSubmission(
            True,
            "submitted",
            int(process_id) if process_id is not None else None,
            **self._submission_timing(response),
        )

    def default_mobile(self) -> str:
        """Return the authenticated account mobile used by the web form."""

        if self._cached_mobile is not None:
            return self._cached_mobile
        if self._configured_mobile:
            self._cached_mobile = self._configured_mobile
            return self._cached_mobile
        response = self._get(DETAIL_MOBILE_ENDPOINT, timeout=20)
        data = self._json(response, "account contact")
        mobile = data.get("mobile")
        self._cached_mobile = str(mobile) if mobile else ""
        return self._cached_mobile

    def prepare_contact(self) -> str:
        """Warm the contact value before the opening-time wait when possible."""

        bridge = getattr(self.session, "_fudan_cas_bridge", None)
        if self._configured_mobile:
            return self.default_mobile()
        if bridge is not None:
            return self.default_mobile()
        return ""

    @staticmethod
    def _submission_timing(response: requests.Response) -> dict[str, object]:
        headers = getattr(response, "headers", {}) or {}

        def integer(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        completed = headers.get("X-Fudan-Challenge-Completed")
        return {
            "submit_elapsed_ms": integer("X-Fudan-Submit-Elapsed-Ms"),
            "first_post_elapsed_ms": integer("X-Fudan-First-Post-Elapsed-Ms"),
            "retry_post_elapsed_ms": integer("X-Fudan-Retry-Post-Elapsed-Ms"),
            "final_post_elapsed_ms": integer("X-Fudan-Final-Post-Elapsed-Ms"),
            "challenge_elapsed_ms": integer("X-Fudan-Challenge-Elapsed-Ms"),
            "challenge_completed": (
                completed.lower() == "true"
                if isinstance(completed, str)
                else None
            ),
            "challenge_completion_signal": headers.get(
                "X-Fudan-Challenge-Completion-Signal"
            ),
            "retry_challenge_elapsed_ms": integer(
                "X-Fudan-Retry-Challenge-Elapsed-Ms"
            ),
            "retry_challenge_completed": (
                str(headers.get("X-Fudan-Retry-Challenge-Completed", "")).lower()
                == "true"
                if headers.get("X-Fudan-Retry-Challenge-Completed") is not None
                else None
            ),
            "retry_challenge_completion_signal": headers.get(
                "X-Fudan-Retry-Challenge-Completion-Signal"
            ),
        }

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
                if operation == "booking submission":
                    raise AntiBotChallenge(
                        "预约提交 HTTP 412：预约接口额外要求瑞数/预约验证；"
                        "当前无浏览器验证码令牌，未能完成提交"
                    ) from exc
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
            available_ids: list[int] = []
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
                    available_ids.append(sub_resource_id)
            periods.append(
                PeriodAvailability(
                    period_id=period_id,
                    time=label,
                    available_sub_resources=available_count,
                    total_sub_resources=len(sub_resource_ids),
                    available_sub_resource_ids=tuple(available_ids),
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
            status_name = str(item.get("status_name") or "")
            if status_name in {"已取消", "已结束", "已过期", "已驳回"}:
                continue
            # The API uses is_cancel=1 to mean that cancellation is available
            # (therefore the reservation is still active); is_cancel=0 is the
            # cancelled/non-cancellable state.  Older responses without a
            # status name still use status=0 for the cancelled state.
            if item.get("is_cancel") is not None and str(item.get("is_cancel")) != "1":
                continue
            if not status_name and str(item.get("status")) == "0":
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
