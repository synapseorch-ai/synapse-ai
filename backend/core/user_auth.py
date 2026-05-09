"""
User authentication utilities for the Synapse login gate.
Provides bcrypt password hashing and HS256 JWT session tokens.
"""
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
