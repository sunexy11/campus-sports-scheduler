from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from .auth import UISCredentials
from .booking_api import BookingReadClient
from .config import load_config
from .errors import BookingError, ConfigurationError


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
        "unfinished_reservation_count": len(client.list_unfinished()),
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
