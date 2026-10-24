"""Expected service failures (ARCHITECTURE.md §4.1).

The bot maps ``err.code`` to ``t(lang, "err.<code>", **err.ctx)``.
"""

from __future__ import annotations


class ServiceError(Exception):
    code: str = "error"

    def __init__(self, message: str = "", **ctx: object) -> None:
        super().__init__(message or self.code)
        self.message = message
        self.ctx: dict[str, object] = dict(ctx)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, ctx={self.ctx!r})"
        )


class NotFoundError(ServiceError):
    code = "not_found"


class PermissionDeniedError(ServiceError):
    code = "forbidden"


class InputError(ServiceError):
    """ctx: field=<name>."""

    code = "invalid"


class InvalidPhoneError(InputError):
    code = "invalid_phone"


class InvalidNameError(InputError):
    code = "invalid_name"


class PhoneRequiredError(ServiceError):
    code = "phone_required"


class SlotTakenError(ServiceError):
    code = "slot_taken"


class SlotUnavailableError(ServiceError):
    code = "slot_unavailable"


class BarberInactiveError(ServiceError):
    code = "barber_inactive"


class ClientLimitError(ServiceError):
    """ctx: kind="max_active"|"max_per_day"|"overlap", limit=int."""

    code = "client_limit"


class UserBlockedError(ServiceError):
    code = "user_blocked"


class InvalidTransitionError(ServiceError):
    """ctx: current=<status value>."""

    code = "invalid_transition"


class TooEarlyError(ServiceError):
    code = "too_early"


class TooLateError(ServiceError):
    code = "too_late"


class InsufficientBonusError(ServiceError):
    code = "no_bonus"


class InvalidInviteError(ServiceError):
    code = "invalid_invite"


class ProposalLimitError(ServiceError):
    code = "proposal_limit"


class ConsentRequiredError(ServiceError):
    code = "consent_required"


ALL_ERRORS: tuple[type[ServiceError], ...] = (
    ServiceError,
    NotFoundError,
    PermissionDeniedError,
    InputError,
    InvalidPhoneError,
    InvalidNameError,
    PhoneRequiredError,
    SlotTakenError,
    SlotUnavailableError,
    BarberInactiveError,
    ClientLimitError,
    UserBlockedError,
    InvalidTransitionError,
    TooEarlyError,
    TooLateError,
    InsufficientBonusError,
    InvalidInviteError,
    ProposalLimitError,
    ConsentRequiredError,
)
