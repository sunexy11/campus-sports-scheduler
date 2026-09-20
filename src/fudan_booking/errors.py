class BookingError(RuntimeError):
    """Base error for expected booking failures."""


class AuthenticationExpired(BookingError):
    """The booking or UIS session is no longer valid."""


class AuthenticationFailed(BookingError):
    """UIS authentication did not complete successfully."""


class AntiBotChallenge(AuthenticationFailed):
    """The booking service returned its JavaScript anti-bot challenge."""


class MFARequired(AuthenticationFailed):
    """The account requires an MFA method that has not been configured."""


class CapacityReached(BookingError):
    """The account already has the maximum unfinished reservations."""


class SlotUnavailable(BookingError):
    """The requested slot was no longer available at submission time."""


class RateLimited(BookingError):
    """The remote service rejected requests because of rate limiting."""


class ConfigurationError(ValueError):
    """The project configuration is invalid."""
