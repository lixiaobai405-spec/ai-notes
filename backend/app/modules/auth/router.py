from datetime import timedelta
from uuid import UUID
from fastapi import APIRouter, Request, Response, Depends, Query
from sqlalchemy import select, update
from app.shared.dependencies import DB, CurrentUser, Admin, preauth_csrf, tab_preauth_csrf, auth_rate_limit
from app.shared.models import User, AuthSession, Invite, ResetToken, Audit
from app.shared.security import token, digest, session_csrf
from app.shared.db import utcnow
from app.shared.config import settings
from app.shared.permissions import audit
from app.shared.errors import AppError
from app.modules.auth import schemas as s, service

router = APIRouter()
public_guards = [Depends(preauth_csrf), Depends(auth_rate_limit)]
tab_public_guards = [Depends(tab_preauth_csrf), Depends(auth_rate_limit)]


def set_auth_cookies(response, raw, csrf):
    age = settings().session_days * 86400
    options = dict(secure=settings().cookie_secure, samesite="lax", max_age=age, path="/")
    response.set_cookie("notes_session", raw, httponly=True, **options)
    response.set_cookie("notes_csrf", csrf, httponly=False, **options)


@router.get("/auth/csrf", response_model=s.CsrfOut)
def csrf(request: Request, response: Response, db: DB):
    cfg = settings()
    origin = request.headers.get("origin")
    if (origin is not None and origin != cfg.app_origin
            or request.headers.get("sec-fetch-site") == "cross-site"):
        raise AppError(403, "origin_invalid", "请求来源不受信任")
    raw = request.cookies.get("notes_session", "")
    session = None
    if raw:
        # Only the session row is locked. Account revocation locks User first;
        # never acquire a User lock here and reverse that lock order.
        session = db.scalar(select(AuthSession).join(User).where(
            AuthSession.token_hash == digest(raw), AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > utcnow(), User.status == "active",
        ).with_for_update(of=AuthSession))
    max_age = None
    if session and session.expires_at > utcnow():
        value = session_csrf(raw)
        # Legacy random CSRF values converge to this same value even when two
        # tabs bootstrap concurrently with a missing or stale cookie.
        session.csrf_hash = digest(value)
        max_age = max(0, int((session.expires_at - utcnow()).total_seconds()))
        db.commit()
    else:
        db.rollback()
        value = request.cookies.get("notes_csrf") or token()
    response.set_cookie("notes_csrf", value, httponly=False, secure=cfg.cookie_secure,
                        samesite="lax", path="/", max_age=max_age)
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": value}


@router.post("/auth/register", response_model=s.UserOut, status_code=201, dependencies=public_guards)
def register(data: s.RegisterIn, request: Request, response: Response, db: DB):
    user, raw, csrf_value = service.register(db, data, request.state.request_id)
    set_auth_cookies(response, raw, csrf_value)
    return user


@router.post("/auth/login", response_model=s.UserOut, dependencies=public_guards)
def login(data: s.LoginIn, request: Request, response: Response, db: DB):
    user, raw, csrf_value = service.login(db, data, request.state.request_id)
    set_auth_cookies(response, raw, csrf_value)
    return user


@router.post("/auth/tab/login", response_model=s.TabSessionOut, dependencies=tab_public_guards)
def tab_login(data: s.LoginIn, request: Request, db: DB):
    user, raw, csrf_value = service.login(db, data, request.state.request_id)
    return {"user": user, "session_token": raw, "csrf_token": csrf_value}


@router.post("/auth/tab/register", response_model=s.TabSessionOut, status_code=201, dependencies=tab_public_guards)
def tab_register(data: s.RegisterIn, request: Request, db: DB):
    user, raw, csrf_value = service.register(db, data, request.state.request_id)
    return {"user": user, "session_token": raw, "csrf_token": csrf_value}


@router.get("/auth/me", response_model=s.UserOut)
def me(user: CurrentUser):
    return user


@router.post("/auth/logout", response_model=s.OkOut)
def logout(request: Request, response: Response, db: DB, user: CurrentUser):
    request.state.auth_session.revoked_at = utcnow()
    audit(db, user.id, "auth.logout", request.state.request_id, user.id)
    db.commit()
    if not request.state.tab_session:
        response.delete_cookie("notes_session", path="/")
        response.delete_cookie("notes_csrf", path="/")
    return {"ok": True}


@router.post("/auth/password/reset", response_model=s.OkOut, dependencies=public_guards)
def reset(data: s.ResetIn, request: Request, db: DB):
    service.reset_password(db, data, request.state.request_id)
    return {"ok": True}


@router.post("/auth/tab/password/reset", response_model=s.OkOut, dependencies=tab_public_guards)
def tab_reset(data: s.ResetIn, request: Request, db: DB):
    service.reset_password(db, data, request.state.request_id)
    return {"ok": True}


@router.get("/admin/invites", response_model=list[s.InviteOut])
def invites(db: DB, admin: Admin, limit: int = Query(50, ge=1, le=100)):
    return db.scalars(select(Invite).order_by(Invite.created_at.desc(), Invite.id).limit(limit)).all()


@router.post("/admin/invites", response_model=s.CreatedInvite, status_code=201)
def create_invite(data: s.InviteIn, request: Request, db: DB, admin: Admin):
    code = token()
    invite = Invite(creator_id=admin.id, code_hash=digest(code), label=data.label, max_uses=data.max_uses, expires_at=utcnow() + timedelta(days=data.valid_days))
    db.add(invite)
    db.flush()
    audit(db, admin.id, "invite.create", request.state.request_id, invite.id)
    db.commit()
    return {**s.InviteOut.model_validate(invite).model_dump(), "code": code}


@router.patch("/admin/invites/{invite_id}", response_model=s.InviteOut)
def patch_invite(invite_id: UUID, data: s.InvitePatch, request: Request, db: DB, admin: Admin):
    invite = db.scalar(select(Invite).where(Invite.id == invite_id).with_for_update())
    if not invite:
        raise AppError(404, "not_found", "邀请码不存在")
    invite.disabled = data.disabled
    audit(db, admin.id, "invite.update", request.state.request_id, invite.id, {"disabled": data.disabled})
    db.commit()
    return invite


@router.get("/admin/users", response_model=list[s.UserOut])
def users(db: DB, admin: Admin, limit: int = Query(50, ge=1, le=100)):
    return db.scalars(select(User).order_by(User.created_at.desc(), User.id).limit(limit)).all()


@router.patch("/admin/users/{user_id}", response_model=s.UserOut)
def patch_user(user_id: UUID, data: s.UserPatch, request: Request, db: DB, admin: Admin):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user:
        raise AppError(404, "not_found", "用户不存在")
    if user.role == "admin":
        raise AppError(400, "protected_admin", "不能从网页停用管理员")
    user.status = data.status
    if data.status == "disabled":
        db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, admin.id, "user.status", request.state.request_id, user.id, {"status": data.status})
    db.commit()
    return user


@router.post("/admin/users/{user_id}/reset-token", response_model=s.ResetOut)
def create_reset(user_id: UUID, request: Request, db: DB, admin: Admin):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user or user.role == "admin":
        raise AppError(400, "reset_unavailable", "管理员账户请通过服务器命令行恢复")
    db.execute(update(ResetToken).where(ResetToken.user_id == user.id, ResetToken.used_at.is_(None)).values(used_at=utcnow()))
    raw, expiry = token(), utcnow() + timedelta(minutes=30)
    db.add(ResetToken(user_id=user.id, token_hash=digest(raw), expires_at=expiry))
    audit(db, admin.id, "user.reset_token", request.state.request_id, user.id)
    db.commit()
    return {"token": raw, "expires_at": expiry}


@router.get("/admin/audit", response_model=list[s.AuditOut])
def audits(db: DB, admin: Admin, limit: int = Query(50, ge=1, le=100)):
    return db.scalars(select(Audit).order_by(Audit.created_at.desc(), Audit.id).limit(limit)).all()
