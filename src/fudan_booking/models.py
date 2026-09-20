from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SlotKey:
    """A user-visible slot; the server chooses the concrete sub-court."""

    venue: str
    sport: str
    date: date
    time: str


@dataclass(frozen=True, slots=True)
class BlockPreference:
    """An ordered preferred block at one venue and for one sport."""

    venue: str
    sport: str
    times: tuple[str, ...]
    priority: int


@dataclass(frozen=True, slots=True)
class BookingPlan:
    selected: tuple[SlotKey, ...]
    unfinished_before: int
    capacity_before: int

    @property
    def will_book(self) -> bool:
        return bool(self.selected)

