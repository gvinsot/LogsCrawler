"""File-based user management for PulsarCD.

Stores users in /data/users.json with bcrypt password hashing.
Auto-creates a default admin user on first boot.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import List, Optional

import bcrypt as _bcrypt
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

# Minimum length accepted for the bootstrap admin password. Anything shorter,
# empty, or a well-known placeholder is refused: the deployment gets a random
# password instead so it can never end up with guessable credentials.
MIN_ADMIN_PASSWORD_LENGTH = 12

# Placeholder values shipped in the sample compose/.env files.
_PLACEHOLDER_PASSWORDS = frozenset({"changeme", "change-me", "changemenow",
                                    "password", "admin", "pulsarcd", "secret"})


def weak_password_reason(password: str) -> Optional[str]:
    """Return why a password is unacceptable, or None if it is fine.

    Applied at bootstrap AND on every write path (create_user / update_user):
    enforcing it only at bootstrap left the documented "change the password from
    the UI after the first boot" flow free to set `changeme` back a second later.
    """
    if not password:
        return "not set"
    if password.strip().lower() in _PLACEHOLDER_PASSWORDS:
        return "a well-known placeholder value"
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        return f"shorter than {MIN_ADMIN_PASSWORD_LENGTH} characters"
    return None


# Kept as a private alias: the bootstrap path already reads this name.
_weak_admin_password_reason = weak_password_reason


def _rejected_password_error(reason: str) -> ValueError:
    """Build the ValueError the admin API turns into an HTTP 400."""
    return ValueError(
        f"Password rejected: it is {reason}. Use at least "
        f"{MIN_ADMIN_PASSWORD_LENGTH} characters and avoid placeholder values."
    )


def _next_token_epoch(current: int = 0) -> int:
    """Return a strictly increasing revocation epoch.

    A wall-clock second is used as the base so that an account recreated under a
    name that existed before does not restart from a value an old token could
    match; ``current + 1`` keeps the sequence strictly increasing when two
    revocations happen within the same second.
    """
    try:
        current_value = int(current)
    except (TypeError, ValueError):
        current_value = 0
    return max(int(time.time()), current_value + 1)


class User(BaseModel):
    """User account."""
    username: str
    password_hash: str
    role: str = "admin"  # "admin" or "viewer"
    # Revocation epoch carried by every JWT issued for this account.  A token
    # whose epoch is older than this value is refused, so bumping the field
    # invalidates every session already open for the user.  Absent from a
    # users.json written by an older version: it then reads as 0, which keeps
    # tokens issued before the upgrade valid until the first revocation.
    token_epoch: int = 0


class UserManager:
    """File-based user CRUD with bcrypt authentication."""

    def __init__(self, path: str = "/data/users.json"):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._users: List[User] = []
        self._load()

    def _load(self) -> None:
        """Load users from JSON file, create default admin if absent."""
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._users = [User(**u) for u in raw]
                logger.info("Users loaded", path=str(self._path), count=len(self._users))
                return
            except Exception as e:
                logger.error("Failed to parse users file, starting fresh",
                             path=str(self._path), error=str(e))

        # Auto-create default admin from env vars or generated password.
        # A weak or default PULSARCD_AUTH__PASSWORD is never accepted: the
        # service is typically published on the public internet, so an
        # unusable-but-random password is safer than a guessable one.
        username = os.environ.get("PULSARCD_AUTH__USERNAME", "admin")
        password = os.environ.get("PULSARCD_AUTH__PASSWORD", "")
        weak_reason = _weak_admin_password_reason(password)
        if weak_reason:
            password = secrets.token_urlsafe(24)
            # Logged exactly once, on account creation: only the bcrypt hash is
            # persisted, so this line is the single chance to copy the value.
            logger.warning(
                "PULSARCD_AUTH__PASSWORD rejected; generated a random admin "
                "password instead. ACTION REQUIRED: copy it from this log now, "
                "sign in, and change it via the UI — or set "
                "PULSARCD_AUTH__PASSWORD to a strong value "
                f"(>= {MIN_ADMIN_PASSWORD_LENGTH} characters), delete the users "
                "file and restart.",
                username=username,
                reason=weak_reason,
                generated_password=password,
            )
        self._users = [User(
            username=username,
            password_hash=_bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode(),
            role="admin",
            token_epoch=_next_token_epoch(),
        )]
        self._save_sync()
        logger.info("Default admin user created", username=username, path=str(self._path))

    def _save_sync(self) -> None:
        """Write users to JSON file (synchronous)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = [u.model_dump() for u in self._users]
            self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error("Failed to save users file", path=str(self._path), error=str(e))
            raise

    async def _save(self) -> None:
        """Write users to JSON file (async-safe)."""
        self._save_sync()

    def authenticate(self, username: str, password: str) -> Optional[User]:
        """Verify credentials and return user if valid."""
        for user in self._users:
            if user.username == username:
                if _bcrypt.checkpw(password.encode(), user.password_hash.encode()):
                    return user
                return None
        return None

    def get_user(self, username: str) -> Optional[User]:
        """Get user by username."""
        for user in self._users:
            if user.username == username:
                return user
        return None

    def token_epoch_for(self, username: str) -> Optional[int]:
        """Return the current token epoch of a user, or None if it does not exist.

        None means every token bearing that username must be refused: the
        account was deleted while its JWT was still within its expiry window.
        """
        user = self.get_user(username)
        if user is None:
            return None
        return int(getattr(user, "token_epoch", 0) or 0)

    def list_users(self) -> List[dict]:
        """List all users (without password hashes)."""
        return [{"username": u.username, "role": u.role} for u in self._users]

    async def create_user(self, username: str, password: str, role: str = "viewer") -> dict:
        """Create a new user.

        Raises ValueError if the username exists, the role is unknown, or the
        password does not meet the policy.
        """
        reason = weak_password_reason(password)
        if reason:
            raise _rejected_password_error(reason)
        async with self._lock:
            if self.get_user(username):
                raise ValueError(f"User '{username}' already exists")
            if role not in ("admin", "viewer"):
                raise ValueError(f"Invalid role: {role}")

            user = User(
                username=username,
                password_hash=_bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode(),
                role=role,
                # Start above any epoch a token issued for a previous account
                # of the same name could carry.
                token_epoch=_next_token_epoch(),
            )
            self._users.append(user)
            await self._save()
            logger.info("User created", username=username, role=role)
            return {"username": user.username, "role": user.role}

    async def update_user(self, username: str, password: Optional[str] = None, role: Optional[str] = None) -> dict:
        """Update an existing user. Raises ValueError if not found.

        A password or role change bumps the token epoch, which immediately
        invalidates every JWT already issued for the account.  A new password
        must satisfy the same policy as the bootstrap one.
        """
        if password is not None:
            reason = weak_password_reason(password)
            if reason:
                raise _rejected_password_error(reason)
        async with self._lock:
            user = self.get_user(username)
            if not user:
                raise ValueError(f"User '{username}' not found")
            revoke = False
            if role is not None:
                if role not in ("admin", "viewer"):
                    raise ValueError(f"Invalid role: {role}")
                revoke = role != user.role
                user.role = role
            if password is not None:
                user.password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
                revoke = True
            if revoke:
                user.token_epoch = _next_token_epoch(user.token_epoch)
            await self._save()
            logger.info("User updated", username=username, role=user.role,
                        sessions_revoked=revoke)
            return {"username": user.username, "role": user.role}

    async def delete_user(self, username: str) -> bool:
        """Delete a user. Raises ValueError if not found or last admin.

        Deleting also cuts the account's live sessions: ``token_epoch_for``
        returns None for an unknown user, and the API refuses every token whose
        subject it cannot resolve.
        """
        async with self._lock:
            user = self.get_user(username)
            if not user:
                raise ValueError(f"User '{username}' not found")

            admin_count = sum(1 for u in self._users if u.role == "admin")
            if user.role == "admin" and admin_count <= 1:
                raise ValueError("Cannot delete the last admin user")

            # Bumped before removal so that a same-named account recreated
            # within the same second cannot inherit a still-valid epoch.
            user.token_epoch = _next_token_epoch(user.token_epoch)
            self._users = [u for u in self._users if u.username != username]
            await self._save()
            logger.info("User deleted", username=username)
            return True
