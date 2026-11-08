"""
User authentication utilities for the Synapse login gate.
Provides bcrypt password hashing and HS256 JWT session tokens.
"""
import hashlib
import hmac
import os
import time
import bcrypt
import jwt
from typing import Optional

_ALG = "HS256"
_EXP = 86400 * 7  # 7 days


def get_jwt_secret() -> str:
    s = os.getenv("SYNAPSE_JWT_SECRET", "")
    if not s:
        raise RuntimeError("SYNAPSE_JWT_SECRET is not configured")
    return s


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, hashed: str) -> bool:
    if not hashed:
        return False
    return bcrypt.checkpw(plaintext.encode(), hashed.encode())


def create_session_token(username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + _EXP,
        "iss": "synapse",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=_ALG)


def verify_session_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[_ALG], options={"verify_iss": True}, issuer="synapse")
        return payload.get("sub")
    except Exception:
        return None


# ── The access link (login off) ─────────────────────────────────────────────
#
# With login off, Synapse is still not open to whoever can reach its port: the
# launcher and the Docker entrypoint generate SYNAPSE_ACCESS_TOKEN and hand out
# a link carrying it (`http://host:3000/?token=…`), Jupyter-style. Presenting
# it once mints a session like a password login does. The session carries the
# token's fingerprint, so rotating the token signs out everyone who used the
# old one, and it is worthless once login is turned on — a password always
# wins over the link.

ACCESS_SUBJECT = "access-link"


def access_token() -> str:
    """This install's access token, or "" when none is configured."""
    return os.getenv("SYNAPSE_ACCESS_TOKEN", "")


def access_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def access_token_matches(presented: str) -> bool:
    expected = access_token()
    return bool(expected) and bool(presented) and hmac.compare_digest(presented, expected)


def create_access_session() -> str:
    now = int(time.time())
    payload = {
        "sub": ACCESS_SUBJECT,
        "atf": access_fingerprint(access_token()),
        "iat": now,
        "exp": now + _EXP,
        "iss": "synapse",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=_ALG)


def session_claims(token: str) -> Optional[dict]:
    """The session's claims if it is ours and unexpired, else None."""
    if not token:
        return None
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=[_ALG],
                          options={"verify_iss": True}, issuer="synapse")
    except Exception:
        return None
