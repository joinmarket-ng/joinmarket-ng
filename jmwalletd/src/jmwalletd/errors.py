"""Custom exception types for the wallet daemon API.

Each exception maps to a specific HTTP status code and error message,
matching the reference JoinMarket implementation's error semantics.
"""

from __future__ import annotations


class JMWalletDaemonError(Exception):
    """Base exception for all wallet daemon errors."""

    status_code: int = 500
    detail: str = "Internal server error."

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.__class__.detail
        super().__init__(self.detail)


class InvalidRequestFormat(JMWalletDaemonError):
    """400 - Malformed or invalid request body."""

    status_code = 400
    detail = "Invalid request format."


class ActionNotAllowed(JMWalletDaemonError):
    """400 - The requested action is not allowed in the current state."""

    status_code = 400
    detail = "Action not allowed."


class InvalidCredentials(JMWalletDaemonError):
    """401 - Wrong password or credentials."""

    status_code = 401
    detail = "Invalid credentials."


class UnlockBackoff(JMWalletDaemonError):
    """429 - Password retry delay is active for this wallet file."""

    status_code = 429
    detail = "Too many failed unlock attempts. Please try again later."


class WalletLifecycleQueueFull(JMWalletDaemonError):
    """429 - Unauthenticated wallet lifecycle admission is at capacity."""

    status_code = 429
    detail = "Too many wallet operations in progress. Please try again later."


class InvalidToken(JMWalletDaemonError):
    """401 - Bearer token is invalid, expired, or missing."""

    status_code = 401
    detail = "Invalid token."


class InsufficientScope(JMWalletDaemonError):
    """403 - Token does not have the required scope."""

    status_code = 403
    detail = "Insufficient scope."


class NoWalletFound(JMWalletDaemonError):
    """404 - No wallet is currently loaded."""

    status_code = 404
    detail = "No wallet loaded."


class WalletNotFound(JMWalletDaemonError):
    """404 - The requested wallet file does not exist."""

    status_code = 404
    detail = "Wallet file not found."


class ServiceAlreadyStarted(JMWalletDaemonError):
    """401 - Maker/taker service is already running."""

    status_code = 401
    detail = "Service already started."


class ServiceNotStarted(JMWalletDaemonError):
    """401 - Cannot stop a service that is not running."""

    status_code = 401
    detail = "Service cannot be stopped as it is not running."


class WalletAlreadyUnlocked(JMWalletDaemonError):
    """401 - Another wallet is already unlocked."""

    status_code = 401
    detail = "Wallet already unlocked."


class WalletAlreadyExists(JMWalletDaemonError):
    """409 - The wallet file already exists and cannot be overwritten."""

    status_code = 409
    detail = "Wallet file cannot be overwritten."


class LockExists(JMWalletDaemonError):
    """409 - A lock file prevents the operation."""

    status_code = 409
    detail = "Wallet cannot be created/opened, it is locked."


class ConfigNotPresent(JMWalletDaemonError):
    """409 - Required config section/field does not exist."""

    status_code = 409
    detail = "Action cannot be performed, config vars are not set."


class TransactionFailed(JMWalletDaemonError):
    """409 - The transaction could not be completed."""

    status_code = 409
    detail = "Transaction failed."


class NotEnoughCoinsForMaker(JMWalletDaemonError):
    """409 - No confirmed coins available to start maker."""

    status_code = 409
    detail = "Maker could not start, no confirmed coins."


class NotEnoughCoinsForTumbler(JMWalletDaemonError):
    """409 - No confirmed coins available to start tumbler."""

    status_code = 409
    detail = "Tumbler could not start, no confirmed coins."


class YieldGeneratorDataUnreadable(JMWalletDaemonError):
    """404 - The yield generator report file is not available."""

    status_code = 404
    detail = "Yield generator report not available."


class BackendNotReady(JMWalletDaemonError):
    """503 - The blockchain backend is not available."""

    status_code = 503
    detail = "Backend daemon not available."
