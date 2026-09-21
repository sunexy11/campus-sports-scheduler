import json
from argparse import Namespace
from datetime import date

import pytest

from fudan_booking import cli
from fudan_booking.booking_api import (
    PeriodAvailability,
    ResourceAvailability,
    ResourceSummary,
)


class FakeReadClient:
    def list_resources(self):
        return [ResourceSummary(938, "北区体育馆-羽毛球", 6)]

    def list_unfinished(self):
        return [{"private": "must not be printed"}]

    def get_availability(self, resource_id, target_date):
        assert resource_id == 938
        assert target_date == date(2026, 9, 22)
        return ResourceAvailability(
            resource_id=938,
            target_date=target_date,
            sub_resource_ids=(939, 940),
            periods=(PeriodAvailability(3800, "19:00-20:00", 1, 2),),
        )


def test_probe_prints_only_safe_read_only_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.BookingReadClient, "login", lambda credentials: FakeReadClient())
    monkeypatch.setattr(
        cli,
        "_credentials",
        lambda prompt: cli.UISCredentials("student", "secret-password"),
    )

    result = cli._run_probe(
        Namespace(
            prompt_credentials=True,
            resource_id=938,
            date=date(2026, 9, 22),
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["mode"] == "read_only"
    assert payload["unfinished_reservation_count"] == 1
    assert payload["remaining_reservation_capacity"] == 2
    assert payload["resources"][0]["name"] == "北区体育馆-羽毛球"
    assert payload["schedule"]["periods"][0]["available"] is True
    output = json.dumps(payload, ensure_ascii=False)
    assert "secret-password" not in output
    assert "must not be printed" not in output


def test_probe_requires_resource_and_date_together() -> None:
    with pytest.raises(ValueError, match="必须同时提供"):
        cli._run_probe(
            Namespace(
                prompt_credentials=False,
                resource_id=938,
                date=None,
            )
        )


def test_local_env_is_loaded_without_overriding_injected_secret(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".env").write_text(
        "FUDAN_USERNAME=from-file\nFUDAN_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FUDAN_PASSWORD", "injected-password")
    monkeypatch.delenv("FUDAN_USERNAME", raising=False)

    cli._load_local_env()

    assert cli.os.environ["FUDAN_USERNAME"] == "from-file"
    assert cli.os.environ["FUDAN_PASSWORD"] == "injected-password"
