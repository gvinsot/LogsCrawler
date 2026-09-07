"""JWT authentication helpers for PulsarCD."""

from datetime import datetime, timedelta, timezone

import jwt


def create_token(username: str, secret: str, expiry_hours: int = 24, role: str = "viewer",
                 token_epoch: int = 0) -> str:
    """Create a JWT token for the given username and role.

    ``token_epoch`` is the revocation epoch of the account at issuance time (see
    ``UserManager.token_epoch_for``).  The API rejects a token whose epoch is
    older than the account's current one, which is how a password change, a role
    change or an account deletion cuts sessions that are already open.  Tokens
    issued before this field existed carry no ``epoch`` claim and are read as
    epoch 0, which stays valid for as long as the account has never been
    revoked (its stored epoch is also 0).
    """
    payload = {
        "sub": username,
        "role": role,
        "epoch": int(token_epoch),
        "exp": datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    """Decode and validate a JWT token. Raises jwt.PyJWTError on failure."""
    return jwt.decode(token, secret, algorithms=["HS256"])
