from datetime import timedelta
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.shared.db import utcnow
from app.shared.models import User, Invite, AuthSession, ResetToken
from app.shared.security import token, digest, session_csrf, hasher, verify_password, DUMMY_HASH
from app.shared.config import settings
from app.shared.errors import AppError
from app.shared.permissions import audit


def create_session(db, user):
    raw = token()
    csrf = session_csrf(raw)
    db.add(AuthSession(user_id=user.id, token_hash=digest(raw), csrf_hash=digest(csrf), expires_at=utcnow() + timedelta(days=settings().session_days)))
    return raw, csrf


def register(db, data, request_id):
    password_hash = hasher.hash(data.password)
    invite = db.scalar(select(Invite).where(Invite.code_hash == digest(data.invite_code)).with_for_update())
    if not invite or invite.disabled or invite.expires_at <= utcnow() or invite.used_count >= invite.max_uses:
        raise AppError(400, "invalid_invite", "邀请码无效、已用完或已过期")
    if db.scalar(select(User.id).where(User.username == data.username)):
        raise AppError(409, "username_taken", "用户名已被使用")
    user = User(username=data.username, display_name=data.display_name, password_hash=password_hash)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "username_taken", "用户名已被使用")
    invite.used_count += 1
    raw, csrf = create_session(db, user)
    audit(db, user.id, "auth.register", request_id, user.id)
    db.commit()
    return user, raw, csrf


def login(db, data, request_id):
    user = db.scalar(select(User).where(User.username == data.username).with_for_update())
    good = verify_password(user.password_hash if user else DUMMY_HASH, data.password)
    if not good or not user or user.status != "active":
        raise AppError(401, "invalid_login", "用户名或密码不正确，或账户已停用")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(data.password)
    raw, csrf = create_session(db, user)
    audit(db, user.id, "auth.login", request_id, user.id)
    db.commit()
    return user, raw, csrf


def reset_password(db, data, request_id):
    # Lock the account first, as login/disable do, then consume the one-time token.
    reset = db.scalar(select(ResetToken).where(ResetToken.token_hash == digest(data.token)))
    if not reset:
        raise AppError(400, "invalid_reset", "重置凭证无效或已过期")
    user = db.scalar(select(User).where(User.id == reset.user_id).with_for_update())
    reset = db.scalar(select(ResetToken).where(ResetToken.id == reset.id).with_for_update().execution_options(populate_existing=True))
    if reset.used_at or reset.expires_at <= utcnow() or user.status != "active":
        raise AppError(400, "invalid_reset", "重置凭证无效或已过期")
    user.password_hash = hasher.hash(data.password)
    reset.used_at = utcnow()
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, user.id, "auth.password_reset", request_id, user.id)
    db.commit()
