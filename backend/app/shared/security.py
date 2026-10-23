import hashlib
import hmac
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def token():
    return secrets.token_urlsafe(32)


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def session_csrf(raw_session: str):
    # Recoverable per-session token without exposing the HttpOnly login bearer.
    return hmac.new(raw_session.encode(), b"ai-notes:session-csrf:v1", hashlib.sha256).hexdigest()


def verify_password(encoded: str, password: str):
    try:
        return hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False
