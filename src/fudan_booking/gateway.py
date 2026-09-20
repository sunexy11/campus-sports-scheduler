from __future__ import annotations

from typing import Protocol

from .models import SlotKey


class BookingGateway(Protocol):
    """Boundary around live Fudan APIs.

    The implementation will be added after read-only contract verification.
    No cancellation method is intentionally exposed.
    """

    def login(self) -> None: ...

    def list_unfinished_count(self) -> int: ...

    def list_available_slots(self) -> set[SlotKey]: ...

    def book(self, slot: SlotKey) -> None: ...

    def verify_booked(self, slot: SlotKey) -> bool: ...

