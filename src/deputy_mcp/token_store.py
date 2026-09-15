"""Where OAuth credentials live between runs: the OS keychain by default, a file on request.

``deputy-mcp login`` mints an access/refresh token pair that must survive restarts, and
the refresh token rotates on every refresh, so it is written again and again. The
storage is behind the small :class:`TokenStore` protocol so the OAuth flow and the
HTTP transport never care which backend holds the secret:

* :class:`KeyringTokenStore` (**default**) delegates to the ``keyring`` library, which
  uses Windows Credential Manager, the macOS Keychain, or the freedesktop Secret Service
  (GNOME Keyring / KWallet) on Linux. No encryption is implemented here: protecting the
  secret at rest is the operating system's job.
* :class:`FileTokenStore` (**explicit fallback**, ``DEPUTY_TOKEN_STORE=<path>``) writes
  plaintext JSON created with ``0600`` permissions. On Windows those POSIX bits are
  largely symbolic, so this is only for environments with no keychain (containers,
  headless CI-like hosts) and is never selected silently.

Versions before 0.2.0 always used ``~/.deputy-mcp/token.json``. When the keychain store
finds that legacy file it moves the newer token set into the keychain and deletes the
plaintext copy, so an upgrade keeps the user signed in.

No token value is ever logged, placed in an exception, or rendered by ``repr``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from deputy_mcp.errors import DeputyConfigError

__all__ = [
    "KEYRING_SERVICE",
    "FileTokenStore",
    "KeyringTokenStore",
    "OAuthTokens",
    "TokenStore",
    "default_config_dir",
    "legacy_token_path",
    "resolve_token_store",
]

logger = logging.getLogger(__name__)

#: Keychain service name (the "application" an OS credential entry belongs to).
KEYRING_SERVICE = "deputy-mcp"
#: Keychain account name for the single OAuth token set this tool keeps.
KEYRING_USERNAME = "oauth-token"

_NO_KEYCHAIN_HINT = (
    "Unlock or install an OS keychain (Windows Credential Manager, macOS Keychain, or a "
    "Secret Service provider such as GNOME Keyring on Linux). Where none exists, e.g. in "
    "a container, set DEPUTY_TOKEN_STORE to a file path to use the plaintext file store "
    "instead (created owner-only where the OS supports it)."
)


def default_config_dir() -> Path:
    """Per-user directory for deputy-mcp's non-secret local files."""
    return Path.home() / ".deputy-mcp"


def legacy_token_path() -> Path:
    """The plaintext token file every version before 0.2.0 wrote by default."""
    return default_config_dir() / "token.json"


@dataclass(frozen=True)
class OAuthTokens:
    """A resolved OAuth token set bound to one Deputy install.

    Attributes:
        access_token: Bearer token for ``/api/v1`` requests (SECRET).
        refresh_token: Long-life token used to mint new access tokens (SECRET).
        expires_at: Absolute expiry as epoch seconds (``time.time()`` scale).
        base_url: Normalized install origin, e.g. ``https://acme.eu.deputy.com``.

    The ``repr`` redacts both token values so the object is safe to log.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    base_url: str

    def is_expired(self, skew: float = 60.0) -> bool:
        """Whether the token is expired, ``skew`` seconds early so refresh pre-empts it."""
        return time.time() >= (self.expires_at - skew)

    def __repr__(self) -> str:
        return (
            "OAuthTokens(access_token='***', refresh_token='***', "
            f"expires_at={self.expires_at!r}, base_url={self.base_url!r})"
        )

    def to_json(self) -> str:
        """Serialize for storage. The result contains secrets: store it, never print it."""
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "base_url": self.base_url,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> OAuthTokens | None:
        """Parse a stored token set; ``None`` when the payload is missing or malformed."""
        try:
            data: Any = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        access, refresh = data.get("access_token"), data.get("refresh_token")
        base_url, expires_at = data.get("base_url"), data.get("expires_at")
        if not (isinstance(access, str) and isinstance(refresh, str) and isinstance(base_url, str)):
            return None
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float, str)):
            return None
        try:
            expires = float(expires_at)
        except ValueError:
            return None
        return cls(
            access_token=access, refresh_token=refresh, expires_at=expires, base_url=base_url
        )


class TokenStore(Protocol):
    """Persistence for one OAuth token set. Implementations never log token values."""

    @property
    def location(self) -> str:
        """Human-readable, secret-free description of where tokens are kept."""
        ...

    def load(self) -> OAuthTokens | None:
        """Return the stored tokens, or ``None`` when nothing usable is stored."""
        ...

    def save(self, tokens: OAuthTokens) -> None:
        """Persist ``tokens``, replacing any previous set."""
        ...

    def delete(self) -> bool:
        """Remove stored tokens. Returns whether anything was removed."""
        ...


class FileTokenStore:
    """Plaintext JSON on disk — the explicit fallback selected by ``DEPUTY_TOKEN_STORE``.

    The file is created with owner-only ``0600`` permissions (best effort: POSIX bits are
    largely symbolic on Windows). Load errors degrade to ``None`` so a missing or corrupt
    file means "not signed in" rather than a crash.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The on-disk location of the token file."""
        return self._path

    @property
    def location(self) -> str:
        return f"plaintext file {self._path}"

    def load(self) -> OAuthTokens | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        return OAuthTokens.from_json(raw)

    def save(self, tokens: OAuthTokens) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions up front so the secret is never briefly
        # readable by other users, then tighten again in case the file already existed.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(tokens.to_json())
        with contextlib.suppress(OSError):
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def delete(self) -> bool:
        try:
            self._path.unlink()
        except OSError:
            return False
        return True

    def __repr__(self) -> str:
        return f"FileTokenStore(path={self._path!r})"


class KeyringTokenStore:
    """The OS keychain via ``keyring`` — the default token store.

    Args:
        legacy_path: Plaintext token file from an older version to migrate from, or
            ``None`` to skip migration.
    """

    def __init__(self, *, legacy_path: Path | None = None) -> None:
        self._legacy_path = legacy_path

    @property
    def location(self) -> str:
        return f"OS keychain ({_backend_name()})"

    def load(self) -> OAuthTokens | None:
        raw = _keyring("read the OAuth token from", "get_password")
        stored = OAuthTokens.from_json(raw) if isinstance(raw, str) else None
        return self._migrate_legacy_file(stored)

    def save(self, tokens: OAuthTokens) -> None:
        _keyring("save the OAuth token to", "set_password", tokens.to_json())

    def delete(self) -> bool:
        # Look before deleting: backends disagree on what a failed delete raises (macOS
        # reports every Keychain error, a locked keychain included, as "not found"), so
        # the exception type cannot tell "nothing stored" from "could not delete".
        removed = False
        if _keyring("read the OAuth token from", "get_password") is not None:
            _keyring("remove the OAuth token from", "delete_password")
            removed = True
        if self._legacy_path is not None and FileTokenStore(self._legacy_path).delete():
            removed = True
        return removed

    def _migrate_legacy_file(self, stored: OAuthTokens | None) -> OAuthTokens | None:
        """Adopt a pre-0.2.0 plaintext token file, then delete it.

        The file wins only when it holds the newer token set: a server still running
        the old version may have rotated the refresh token into the file after an
        earlier migration. A corrupt file is left untouched rather than destroyed.
        """
        path = self._legacy_path
        if path is None or not path.is_file():
            return stored
        legacy_store = FileTokenStore(path)
        legacy = legacy_store.load()
        if legacy is None:
            return stored
        if stored is None or legacy.expires_at > stored.expires_at:
            self.save(legacy)
            stored = legacy
        legacy_store.delete()
        logger.warning(
            "Moved the OAuth token from the legacy plaintext file into the %s and "
            "deleted the file.",
            self.location,
        )
        return stored

    def __repr__(self) -> str:
        return "KeyringTokenStore()"


def resolve_token_store(token_store_path: Path | None) -> TokenStore:
    """The file store when a path is configured, otherwise the OS keychain."""
    if token_store_path is not None:
        return FileTokenStore(token_store_path)
    return KeyringTokenStore(legacy_path=legacy_token_path())


def _keyring(action: str, method: str, *secret: str) -> str | None:
    """Call ``keyring.<method>`` for this tool's entry, mapping failures to a config error.

    A keychain backend can fail in platform-specific ways (no backend, a locked
    collection, a Windows credential blob that is too large), so every exception is
    translated into one actionable :class:`DeputyConfigError`. Neither the secret nor
    the backend's own message is included, since the latter may echo the value.
    """
    import keyring

    try:
        result: str | None = getattr(keyring, method)(KEYRING_SERVICE, KEYRING_USERNAME, *secret)
    except Exception as exc:
        raise DeputyConfigError(
            f"Could not {action} the OS keychain ({type(exc).__name__}).",
            hint=_NO_KEYCHAIN_HINT,
        ) from exc
    return result


def _backend_name() -> str:
    """Name of the active keyring backend, for messages (never raises)."""
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception:
        return "unavailable"
    return str(getattr(backend, "name", type(backend).__name__))
