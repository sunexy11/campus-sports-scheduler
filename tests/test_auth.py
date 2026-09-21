import base64

import pytest
import requests
from Crypto.PublicKey import RSA

from fudan_booking.auth import (
    UISCredentials,
    _CasBridge,
    _raise_for_status,
    login_to_service,
)
from fudan_booking.errors import AntiBotChallenge, AuthenticationFailed, MFARequired


class FakeResponse:
    def __init__(
        self,
        *,
        url="https://example.invalid",
        payload=None,
        text="",
        status_code=200,
    ) -> None:
        self.url = url
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, password_result: dict) -> None:
        key = RSA.generate(1024).public_key().export_key(format="DER")
        self.public_key = base64.b64encode(key).decode("ascii")
        self.password_result = password_result
        self.posts = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, **kwargs):
        if "authserver/login" in url:
            return FakeResponse(url="https://id.fudan.edu.cn/ac/?lck=test-token")
        return FakeResponse(url=url)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("queryAuthMethods"):
            return FakeResponse(
                payload={
                    "code": 200,
                    "entityId": "booking",
                    "requestType": "chain_type",
                    "data": [
                        {"authChainCode": "chain", "moduleCodes": ["userAndPwd"]}
                    ],
                }
            )
        if url.endswith("getJsPublicKey"):
            return FakeResponse(payload={"code": 200, "data": self.public_key})
        if url.endswith("authExecute"):
            return FakeResponse(payload=self.password_result)
        if url.endswith("authnEngine"):
            return FakeResponse(
                text='var locationValue = "https://booking.fudan.edu.cn/reservation/api/login/cas?ticket=ST-test";'
            )
        raise AssertionError(url)


def test_password_only_login_does_not_require_totp() -> None:
    session = FakeSession({"code": 200, "loginToken": "login-token", "pageLevelNo": 1})
    result = login_to_service(
        UISCredentials("student", "password"),
        "https://booking.example/cas",
        session=session,
    )
    assert result is session
    auth_calls = [call for call in session.posts if call[0].endswith("authExecute")]
    assert len(auth_calls) == 1
    assert auth_calls[0][1]["json"]["authModuleCode"] == "userAndPwd"


def test_missing_totp_is_reported_only_when_mfa_is_requested() -> None:
    session = FakeSession(
        {
            "code": 200,
            "loginToken": None,
            "pageLevelNo": 2,
            "moduleCodes": ["userAndOtp"],
            "requestNumber": "request-1",
        }
    )
    with pytest.raises(MFARequired, match="requires MFA"):
        login_to_service(
            UISCredentials("student", "password"),
            "https://booking.example/cas",
            session=session,
        )


def test_http_error_names_step_without_exposing_ticket_query() -> None:
    session = FakeSession({"code": 200, "loginToken": "login-token", "pageLevelNo": 1})

    def failing_ticket_get(url, **kwargs):
        if "authserver/login" in url:
            return FakeResponse(url="https://id.fudan.edu.cn/ac/?lck=test-token")
        response = requests.Response()
        response.status_code = 500
        response.url = "https://booking.example/cas?ticket=ST-secret-ticket"
        return response

    session.get = failing_ticket_get
    with pytest.raises(AuthenticationFailed) as error:
        login_to_service(
            UISCredentials("student", "password"),
            "https://booking.example/cas",
            session=session,
        )

    assert "CAS 票据兑换 HTTP 500" in str(error.value)
    assert "ST-secret-ticket" not in str(error.value)


def test_booking_412_is_reported_as_antibot_challenge() -> None:
    response = requests.Response()
    response.status_code = 412
    response.url = "https://booking.fudan.edu.cn/reservation/api/login/cas?ticket=secret"
    with pytest.raises(AntiBotChallenge, match="瑞数反爬校验"):
        _raise_for_status(response, "CAS 票据兑换")


def test_bridge_response_copies_booking_timing_metadata() -> None:
    response = _CasBridge._response_from_result(
        {
            "status": 200,
            "content_type": "application/json",
            "body": '{"e":"OK"}',
            "timing": {
                "submit_elapsed_ms": 15123,
                "first_post_elapsed_ms": 12,
                "retry_post_elapsed_ms": 8,
                "challenge_elapsed_ms": 15001,
                "challenge_completed": False,
            },
        },
        "https://booking.fudan.edu.cn/reservation/site/resource/launch",
    )

    assert response.headers["X-Fudan-Submit-Elapsed-Ms"] == "15123"
    assert response.headers["X-Fudan-First-Post-Elapsed-Ms"] == "12"
    assert response.headers["X-Fudan-Retry-Post-Elapsed-Ms"] == "8"
    assert response.headers["X-Fudan-Challenge-Elapsed-Ms"] == "15001"
    assert response.headers["X-Fudan-Challenge-Completed"] == "False"


def test_booking_412_can_use_local_cas_bridge(monkeypatch, tmp_path) -> None:
    session = FakeSession({"code": 200, "loginToken": "login-token", "pageLevelNo": 1})
    bridge_script = tmp_path / "cas_bridge.mjs"
    bridge_script.write_text("placeholder", encoding="utf-8")

    def failing_ticket_get(url, **kwargs):
        if "authserver/login" in url:
            return FakeResponse(url="https://id.fudan.edu.cn/ac/?lck=test-token")
        response = requests.Response()
        response.status_code = 412
        response.url = "https://booking.fudan.edu.cn/reservation/api/login/cas?ticket=secret"
        return response

    session.get = failing_ticket_get
    calls = []

    class FakeBridge:
        def __init__(self, script, node, ticket_url, timeout):
            calls.append((script, node, ticket_url, timeout))

        def close(self):
            return None

    monkeypatch.setenv("FUDAN_CAS_BRIDGE_SCRIPT", str(bridge_script))
    monkeypatch.setattr("fudan_booking.auth._CasBridge", FakeBridge)
    result = login_to_service(
        UISCredentials("student", "password"),
        "https://booking.fudan.edu.cn/reservation/api/login/cas",
        session=session,
    )

    assert result is session
    assert isinstance(session._fudan_cas_bridge, FakeBridge)
    assert calls[0][2].startswith(
        "https://booking.fudan.edu.cn/"
    )
