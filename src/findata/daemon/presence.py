"""User presence: the user confirms at the Mac before the daemon acts with human authority.

Creating a grant, approving an agent's request and removing an Item as `cli` or `web` need
Touch ID, or the login password when Touch ID is unavailable (`deps.presence_gate`). On Linux
and Windows there is no check yet: the operations proceed and are audited as unsupported.
Nothing switches the check off; tests give the daemon a fake checker instead.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Any, Literal, Protocol

from findata.wire import PresenceKind

Presence = Literal["confirmed", "denied", "not_possible", "unsupported"]

# LAPolicyDeviceOwnerAuthentication: Touch ID, or the login password when Touch ID is
# unavailable or locked out (LAPolicyDeviceOwnerAuthenticationWithBiometrics, 1, has no
# password fallback).
DEVICE_OWNER_AUTHENTICATION = 2


class PresenceChecker(Protocol):
    kind: PresenceKind

    def confirm(self, reason: str) -> Presence:
        """Ask the user to confirm `reason`, a lowercase phrase; blocks until answered."""
        ...


def _new_context() -> Any:
    """A real LAContext. The framework is imported here, so only a real check loads it."""
    from LocalAuthentication import LAContext  # pyobjc, installed on macOS only

    return LAContext.alloc().init()


class MacPresence:
    """Touch ID or the login password through LocalAuthentication.

    macOS shows the reason after the process name, so reasons are lowercase phrases.
    """

    kind: PresenceKind = "touch_id"

    def __init__(
        self, timeout_s: float = 60.0, context_factory: Callable[[], Any] | None = None
    ) -> None:
        self._timeout_s = timeout_s
        self._context_factory = context_factory

    def confirm(self, reason: str) -> Presence:
        context = (self._context_factory or _new_context)()
        can_prompt, _ = context.canEvaluatePolicy_error_(DEVICE_OWNER_AUTHENTICATION, None)
        if not can_prompt:  # e.g. no login session that can show the prompt
            return "not_possible"
        answered = threading.Event()
        confirmed = False

        def reply(success: bool, error: Any) -> None:  # called on a framework thread
            nonlocal confirmed
            confirmed = bool(success)
            answered.set()

        context.evaluatePolicy_localizedReason_reply_(DEVICE_OWNER_AUTHENTICATION, reason, reply)
        if not answered.wait(self._timeout_s):
            context.invalidate()  # take the unanswered prompt down
            return "denied"
        return "confirmed" if confirmed else "denied"  # failed or cancelled


class UnsupportedPresence:
    """No presence check on this platform yet: every operation proceeds, audited as such."""

    kind: PresenceKind = "unsupported"

    def confirm(self, reason: str) -> Presence:
        return "unsupported"


def default_presence() -> PresenceChecker:
    """This platform's real check."""
    return MacPresence() if sys.platform == "darwin" else UnsupportedPresence()
