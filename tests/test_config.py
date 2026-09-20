from pathlib import Path

from fudan_booking.config import load_config


def test_example_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "config.example.yaml")
    assert config.max_unfinished_reservations == 3
    assert config.raw["limits"]["auto_cancel"] is False
    assert config.raw["notification"]["provider"] == "qq_smtp"

