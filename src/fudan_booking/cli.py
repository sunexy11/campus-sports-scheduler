from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from .auth import UISCredentials
from .booking_api import BookingReadClient
from .booking_runner import (
    prewarm_scheduled_context,
    scheduled_book_once,
    scheduled_book_with_retries,
)
from .config import load_config
from .errors import BookingError, ConfigurationError
from .monitor import monitor_and_book_once
from .notifier import QQSMTPNotifier, QQSMTPSettings


def _load_local_env() -> None:
    """Load only this project's .env and never override injected secrets."""

    env_path = Path.cwd() / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fudan-booking")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-config", help="validate a YAML config")
    validate.add_argument("--config", required=True)

    probe = commands.add_parser(
        "probe",
        help="perform read-only login and booking API checks",
    )
    probe.add_argument(
        "--prompt-credentials",
        action="store_true",
        help="prompt for the UIS username and password without storing them",
    )
    probe.add_argument("--resource-id", type=int, help="optional resource ID to inspect")
    probe.add_argument("--date", type=date.fromisoformat, help="schedule date in YYYY-MM-DD")

    monitor = commands.add_parser("monitor-once", help="check configured slots once")
    monitor.add_argument("--config", required=True)
    monitor.add_argument("--no-email", action="store_true", help="do not send QQ email")
    monitor.add_argument(
        "--allow-booking",
        action="store_true",
        help="allow jobs with mode=auto_book_if_capacity to submit reservations",
    )
    monitor.add_argument(
        "--max-booking-rounds",
        type=int,
        default=3,
        help="maximum re-scan rounds after racing booking attempts",
    )

    scheduled = commands.add_parser(
        "scheduled-book-once",
        help="evaluate opening-time preferences once",
    )
    scheduled.add_argument("--config", required=True)
    scheduled.add_argument(
        "--allow-booking",
        action="store_true",
        help="submit reservations; without this flag the command is a dry-run",
    )
    scheduled.add_argument(
        "--today",
        type=date.fromisoformat,
        help="override today for a deterministic local test (YYYY-MM-DD)",
    )
    scheduled.add_argument(
        "--wait-until",
        help="wait until HH:MM Asia/Shanghai after login before querying/submitting",
    )
    scheduled.add_argument(
        "--retry-window-seconds",
        type=int,
        default=180,
        help="keep live opening-time retries running for this many seconds",
    )
    scheduled.add_argument("--no-email", action="store_true", help="do not send QQ email")
    return parser


def _credentials(prompt: bool) -> UISCredentials:
    if not prompt:
        return UISCredentials.from_env()

    username = os.getenv("FUDAN_USERNAME") or input("复旦统一身份认证账号：").strip()
    password = getpass.getpass("复旦统一身份认证密码（不会回显）：")
    if not username or not password:
        raise ValueError("账号和密码不能为空")
    return UISCredentials(
        username=username,
        password=password,
        totp_secret=os.getenv("FUDAN_TOTP_SECRET", ""),
    )


def _run_probe(args: argparse.Namespace) -> int:
    if (args.resource_id is None) != (args.date is None):
        raise ValueError("--resource-id 和 --date 必须同时提供")

    client = BookingReadClient.login(_credentials(args.prompt_credentials))
    resources = client.list_resources()
    unfinished_count = len(client.list_unfinished())
    result: dict[str, object] = {
        "ok": True,
        "mode": "read_only",
        "resources": [
            {
                "resource_id": resource.resource_id,
                "name": resource.name,
                "resource_type": resource.resource_type,
            }
            for resource in resources
        ],
        "unfinished_reservation_count": unfinished_count,
        "remaining_reservation_capacity": max(0, 3 - unfinished_count),
        "read_request_timings": getattr(client, "read_request_timings", []),
    }
    if args.resource_id is not None:
        availability = client.get_availability(args.resource_id, args.date)
        result["schedule"] = {
            "resource_id": args.resource_id,
            "date": args.date.isoformat(),
            "sub_resource_count": len(availability.sub_resource_ids),
            "periods": [
                {
                    "period_id": period.period_id,
                    "time": period.time,
                    "available": period.available,
                    "available_sub_resource_count": period.available_sub_resources,
                    "total_sub_resource_count": period.total_sub_resources,
                }
                for period in availability.periods
            ],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _run_monitor_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = BookingReadClient.login(_credentials(False))
    notifier = None
    if not getattr(args, "no_email", False):
        notifier = QQSMTPNotifier(QQSMTPSettings.from_env())
    result = monitor_and_book_once(
        client,
        config.raw,
        notifier,
        allow_booking=bool(getattr(args, "allow_booking", False)),
        max_rounds=int(getattr(args, "max_booking_rounds", 3)),
    )
    result["read_request_timings"] = getattr(client, "read_request_timings", [])
    print(
        json.dumps(
            {"ok": True, "mode": "monitor_once", **result},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_scheduled_book_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = BookingReadClient.login(_credentials(False))
    run_today = args.today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # Do the stable, opening-time-independent work before the optional wait.
    # Availability is intentionally refreshed after the wait because the site
    # may only expose the newly opened date/periods at the opening moment.
    enabled_jobs = [
        job
        for job in config.raw.get("scheduled_jobs", [])
        if job.get("enabled")
        and max(0, int(job.get("max_new_reservations", 1))) > 0
    ]
    prepared_resources = None
    if enabled_jobs:
        try:
            prepared_resources = client.list_resources()
            if args.allow_booking:
                client.prepare_contact()
            prewarm_scheduled_context(
                client,
                config.raw,
                today=run_today,
                resources=prepared_resources,
            )
        except (BookingError, requests.RequestException):
            if not args.allow_booking:
                raise
            # Prewarming is an optimization.  A live booking run retries the
            # same reads after the opening time using the existing session.
            prepared_resources = None
    if args.wait_until:
        hour_text, minute_text = args.wait_until.split(":", 1)
        target_minutes = int(hour_text) * 60 + int(minute_text)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        current_minutes = now.hour * 60 + now.minute
        if target_minutes > current_minutes:
            time.sleep((target_minutes - current_minutes) * 60 - now.second)
    if args.allow_booking:
        result = scheduled_book_with_retries(
            client,
            config.raw,
            today=run_today,
            allow_booking=True,
            prepared_resources=prepared_resources,
            retry_window_seconds=int(args.retry_window_seconds),
        )
    else:
        result = scheduled_book_once(
            client,
            config.raw,
            today=run_today,
            allow_booking=False,
            prepared_resources=prepared_resources,
        )
    if not args.no_email:
        notifier = QQSMTPNotifier(QQSMTPSettings.from_env())
        successful = [
            (job, item)
            for job in result["jobs"]
            for item in job["results"]
            if item["ok"]
        ]
        lines = ["复旦场馆定时预约结果："]
        if successful:
            lines.extend(
                f"- {job['date']} {item['time']}（{job['name']}）"
                for job, item in successful
            )
        elif any(job["mode"] == "dry_run" for job in result["jobs"]):
            lines.append("本次未开启真实预约，未提交任何场次。")
        else:
            lines.append("本次未成功预约任何场次。")
        notifier.send("复旦场馆定时预约结果", "\n".join(lines))
    result["read_request_timings"] = getattr(client, "read_request_timings", [])
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate-config":
            config = load_config(args.config)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "max_unfinished_reservations": config.max_unfinished_reservations,
                        "auto_cancel": False,
                        "notification": "qq_smtp",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "probe":
            return _run_probe(args)
        if args.command == "monitor-once":
            return _run_monitor_once(args)
        if args.command == "scheduled-book-once":
            return _run_scheduled_book_once(args)
    except (ConfigurationError, ValueError) as exc:
        print(f"输入或配置错误：{exc}", file=sys.stderr)
        return 2
    except BookingError as exc:
        print(f"只读联调失败：{exc}", file=sys.stderr)
        return 3
    except requests.RequestException as exc:
        print(f"网络请求失败：{type(exc).__name__}", file=sys.stderr)
        return 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
