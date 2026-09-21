from __future__ import annotations

import atexit
import base64
import html
import json
import os
import re
import selectors
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pyotp
import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from .errors import AntiBotChallenge, AuthenticationFailed, MFARequired

UIS_BASE = "https://id.fudan.edu.cn"
IDP_BASE = f"{UIS_BASE}/idp"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class UISCredentials:
    username: str
    password: str
    totp_secret: str = ""

    @classmethod
    def from_env(cls) -> UISCredentials:
        username = os.getenv("FUDAN_USERNAME", "")
        password = os.getenv("FUDAN_PASSWORD", "")
        if not username or not password:
            raise ValueError("FUDAN_USERNAME and FUDAN_PASSWORD are required")
        return cls(
            username=username,
            password=password,
            totp_secret=os.getenv("FUDAN_TOTP_SECRET", ""),
        )


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return session


def _encrypt_password(password: str, public_key_b64: str) -> str:
    try:
        public_key = RSA.import_key(base64.b64decode(public_key_b64))
        cipher = PKCS1_v1_5.new(public_key)
        encrypted = cipher.encrypt(password.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise AuthenticationFailed("UIS returned an invalid RSA public key") from exc
    return base64.b64encode(encrypted).decode("ascii")


def _response_json(response: requests.Response, step: str) -> dict:
    _raise_for_status(response, step)
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise AuthenticationFailed(f"UIS {step} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthenticationFailed(f"UIS {step} returned an unexpected payload")
    return payload


def _raise_for_status(response: requests.Response, step: str) -> None:
    """Raise a diagnostic auth error without exposing query strings or bodies."""

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        parsed = urlsplit(response.url)
        endpoint = f"{parsed.hostname or 'unknown-host'}{parsed.path}"
        if response.status_code == 412 and parsed.hostname == "booking.fudan.edu.cn":
            raise AntiBotChallenge(
                f"UIS {step} HTTP 412：booking.fudan.edu.cn 启用了瑞数反爬校验；"
                "纯 requests/ehall 请求无法完成 JavaScript 校验"
            ) from exc
        raise AuthenticationFailed(
            f"UIS {step} HTTP {response.status_code} ({endpoint})"
        ) from exc


def _cas_bridge_script() -> Path:
    configured = os.getenv("FUDAN_CAS_BRIDGE_SCRIPT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "challenge" / "cas_bridge.mjs"


class _CasBridge:
    """Persistent, read-only Node bridge for a booking session."""

    def __init__(self, script: Path, node: str, ticket_url: str, timeout: float) -> None:
        self.timeout = timeout
        try:
            self.process = subprocess.Popen(
                [node, str(script)],
                cwd=str(script.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            atexit.register(self.close)
            self._call({"ticket_url": ticket_url})
        except (AuthenticationFailed, OSError, subprocess.TimeoutExpired) as exc:
            self.close()
            if isinstance(exc, AuthenticationFailed):
                raise
            raise AuthenticationFailed("CAS 校验桥启动失败或超时") from exc

    def _call(self, payload: dict) -> dict:
        if self.process.poll() is not None or self.process.stdin is None:
            raise AuthenticationFailed("CAS 校验桥进程已退出")
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            if self.process.stdout is None:
                raise AuthenticationFailed("CAS 校验桥没有标准输出")
            selector = selectors.DefaultSelector()
            try:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(self.timeout):
                    raise AuthenticationFailed("CAS 校验桥启动失败或超时")
                line = self.process.stdout.readline()
            finally:
                selector.close()
        except (BrokenPipeError, OSError) as exc:
            raise AuthenticationFailed("CAS 校验桥进程通信失败") from exc
        try:
            result = json.loads(line.strip())
        except (AttributeError, json.JSONDecodeError) as exc:
            raise AuthenticationFailed("CAS 校验桥返回了无效结果") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise AuthenticationFailed("CAS 校验桥未能完成预约系统请求")
        return result

    def get(self, url: str, headers: dict[str, str]) -> requests.Response:
        result = self._call({"op": "get", "url": url, "headers": headers})
        return self._response_from_result(result, url)

    def book_resource(
        self,
        *,
        group_id: int,
        sub_resource_ids: tuple[int, ...],
        period_id: int,
        target_date: str,
        phone: str,
        number: int,
    ) -> requests.Response:
        result = self._call(
            {
                "op": "book_resource",
                "group_id": group_id,
                "sub_resource_ids": list(sub_resource_ids),
                "period_id": period_id,
                "date": target_date,
                "phone": phone,
                "number": number,
            }
        )
        return self._response_from_result(result, "https://booking.fudan.edu.cn/reservation/site/resource/launch")

    @staticmethod
    def _response_from_result(result: dict, url: str) -> requests.Response:
        response = requests.Response()
        response.status_code = int(result.get("status", 0))
        response.url = url
        response.headers["Content-Type"] = str(result.get("content_type") or "")
        timing = result.get("timing")
        if isinstance(timing, dict):
            for key, header in (
                ("submit_elapsed_ms", "X-Fudan-Submit-Elapsed-Ms"),
                ("first_post_elapsed_ms", "X-Fudan-First-Post-Elapsed-Ms"),
                ("retry_post_elapsed_ms", "X-Fudan-Retry-Post-Elapsed-Ms"),
                ("final_post_elapsed_ms", "X-Fudan-Final-Post-Elapsed-Ms"),
                ("challenge_elapsed_ms", "X-Fudan-Challenge-Elapsed-Ms"),
                ("challenge_completed", "X-Fudan-Challenge-Completed"),
                (
                    "challenge_completion_signal",
                    "X-Fudan-Challenge-Completion-Signal",
                ),
                ("retry_challenge_elapsed_ms", "X-Fudan-Retry-Challenge-Elapsed-Ms"),
                ("retry_challenge_completed", "X-Fudan-Retry-Challenge-Completed"),
                (
                    "retry_challenge_completion_signal",
                    "X-Fudan-Retry-Challenge-Completion-Signal",
                ),
            ):
                value = timing.get(key)
                if value is not None:
                    response.headers[header] = str(value)
        response._content = str(result.get("body") or "").encode("utf-8")
        return response

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=2)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            process.kill()


def _redeem_ticket_with_bridge(client: requests.Session, ticket_url: str) -> _CasBridge:
    """Run the local no-browser bridge and keep the booking session there.

    The ticket is sent over stdin rather than command-line arguments, and neither
    the ticket nor the bridge's stderr is surfaced in application output.
    """

    script = _cas_bridge_script()
    if not script.is_file():
        raise AuthenticationFailed(
            "找不到 CAS 校验桥；请安装 challenge 目录依赖，或设置 FUDAN_CAS_BRIDGE_SCRIPT"
        )
    node = os.getenv("FUDAN_NODE_BIN", "node")
    timeout_seconds = float(os.getenv("FUDAN_CAS_BRIDGE_TIMEOUT", "120"))
    bridge = _CasBridge(script, node, ticket_url, timeout_seconds)
    client._fudan_cas_bridge = bridge
    return bridge


def login_to_service(
    credentials: UISCredentials,
    service: str,
    session: requests.Session | None = None,
) -> requests.Session:
    """Authenticate to a CAS service without browser automation.

    Password-only accounts complete immediately. TOTP is used only when UIS
    explicitly asks for the optional second authentication level.
    """

    client = session or build_session()
    first = client.get(
        f"{UIS_BASE}/authserver/login",
        params={"service": service},
        allow_redirects=True,
        timeout=20,
    )
    _raise_for_status(first, "登录页")
    match = re.search(r"[?&]lck=([\w_-]+)", first.url)
    if not match:
        raise AuthenticationFailed("UIS login did not provide an lck token")
    lck = match.group(1)
    referer = first.url

    methods = _response_json(
        client.post(
            f"{IDP_BASE}/authn/queryAuthMethods",
            json={"lck": lck},
            headers={"Referer": referer},
            timeout=20,
        ),
        "queryAuthMethods",
    )
    if str(methods.get("code")) not in {"200", "4339"}:
        raise AuthenticationFailed("UIS rejected the authentication-method query")

    modules = methods.get("data") or []
    if not isinstance(modules, list) or not modules:
        raise AuthenticationFailed("UIS did not return an authentication chain")
    chain = next(
        (
            item
            for item in modules
            if isinstance(item, dict) and "userAndPwd" in (item.get("moduleCodes") or [])
        ),
        modules[0],
    )
    if not isinstance(chain, dict):
        raise AuthenticationFailed("UIS returned an invalid authentication chain")

    public_key_payload = _response_json(
        client.post(
            f"{IDP_BASE}/authn/getJsPublicKey",
            headers={"Referer": referer},
            timeout=20,
        ),
        "getJsPublicKey",
    )
    if str(public_key_payload.get("code")) != "200":
        raise AuthenticationFailed("UIS did not provide an RSA public key")
    public_key_data = public_key_payload.get("data")
    public_key_b64 = (
        public_key_data.get("data") if isinstance(public_key_data, dict) else public_key_data
    )
    if not isinstance(public_key_b64, str):
        raise AuthenticationFailed("UIS returned an invalid RSA public key payload")

    entity_id = methods.get("entityId") or service.rstrip("/")
    request_type = methods.get("requestType", "chain_type")
    chain_code = chain.get("authChainCode", "")
    password_result = _response_json(
        client.post(
            f"{IDP_BASE}/authn/authExecute",
            json={
                "authModuleCode": "userAndPwd",
                "authChainCode": chain_code,
                "entityId": entity_id,
                "requestType": request_type,
                "lck": lck,
                "authPara": {
                    "loginName": credentials.username,
                    "password": _encrypt_password(credentials.password, public_key_b64),
                    "verifyCode": "",
                },
            },
            headers={"Referer": referer},
            timeout=20,
        ),
        "password authentication",
    )
    if str(password_result.get("code")) != "200":
        raise AuthenticationFailed("UIS rejected the username or password")

    login_token = password_result.get("loginToken")
    if not login_token and password_result.get("pageLevelNo") == 2:
        mfa_modules = password_result.get("moduleCodes") or []
        if not credentials.totp_secret:
            raise MFARequired(f"UIS requires MFA; supported modules: {mfa_modules}")
        if "userAndOtp" not in mfa_modules:
            raise MFARequired(f"UIS MFA does not offer TOTP; supported modules: {mfa_modules}")
        otp_result = _response_json(
            client.post(
                f"{IDP_BASE}/authn/authExecute",
                json={
                    "authModuleCode": "userAndOtp",
                    "authChainCode": password_result.get("authChainCode", chain_code),
                    "entityId": entity_id,
                    "requestType": request_type,
                    "lck": lck,
                    "requestNumber": password_result.get("requestNumber"),
                    "authPara": {
                        "loginName": credentials.username,
                        "otpCode": pyotp.TOTP(credentials.totp_secret).now(),
                        "verifyCode": "",
                    },
                },
                headers={"Referer": referer},
                timeout=20,
            ),
            "TOTP authentication",
        )
        if str(otp_result.get("code")) != "200":
            raise AuthenticationFailed("UIS rejected the TOTP code")
        login_token = otp_result.get("loginToken")

    if not login_token:
        raise AuthenticationFailed("UIS did not return a login token")

    engine = client.post(
        f"{IDP_BASE}/authCenter/authnEngine",
        data={"loginToken": login_token},
        headers={"Referer": referer},
        allow_redirects=False,
        timeout=20,
    )
    _raise_for_status(engine, "CAS 票据生成")
    ticket_match = re.search(r'var\s+locationValue\s*=\s*"([^"]+)"', engine.text)
    if not ticket_match:
        raise AuthenticationFailed("UIS did not return a CAS ticket URL")

    ticket_url = html.unescape(ticket_match.group(1))
    redeemed = client.get(ticket_url, allow_redirects=True, timeout=20)
    try:
        _raise_for_status(redeemed, "CAS 票据兑换")
    except AntiBotChallenge:
        _redeem_ticket_with_bridge(client, ticket_url)
    return client
