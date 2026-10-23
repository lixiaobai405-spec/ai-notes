"""Private conversations and durable, at-most-once provider attempts."""
from datetime import timedelta
import hashlib
import json
import time

from sqlalchemy import func, select, update

from app.modules.ai import chat_provider
from app.modules.ai.models import AiConversation, AiRun, AiStep
from app.modules.ai.schemas import RunOut
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.errors import AppError
from app.shared.models import AuthSession, User

ACTIVE = ("queued", "running")
SYSTEM = "你是知页的学习助手。用清楚准确的语言回答用户，缺少依据时说明不确定。"


def conversation(db, owner, conversation_id):
    row = db.scalar(select(AiConversation).where(AiConversation.id == conversation_id, AiConversation.owner_id == owner))
    if not row:
        raise AppError(404, "conversation_not_found", "对话不存在")
    return row


def get_run(db, owner, run_id):
    row = db.scalar(select(AiRun).where(AiRun.id == run_id, AiRun.owner_id == owner))
    if not row:
        raise AppError(404, "run_not_found", "对话运行不存在")
    return row


def runs(db, owner, conversation_id):
    return list(db.scalars(select(AiRun).where(AiRun.owner_id == owner, AiRun.conversation_id == conversation_id)
                           .order_by(AiRun.created_at, AiRun.id)))


def create_run(db, owner, conversation_id, data):
    parent = conversation(db, owner, conversation_id)
    payload = data.model_dump(mode="json")
    if data.image is None:
        payload.pop("image")  # Preserve pre-image request hashes for existing text/PDF retries.
    request_hash = hashlib.sha256(json.dumps({"conversation": str(conversation_id), **payload},
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = db.scalar(select(AiRun).where(AiRun.owner_id == owner, AiRun.request_key == data.request_key))
    if previous:
        if previous.request_hash != request_hash:
            raise AppError(409, "request_key_conflict", "这次提交的内容已改变，请重新发送")
        return previous
    if not settings().deepseek_api_key.get_secret_value():
        raise AppError(503, "ai_not_configured", "共享模型服务尚未配置，请联系管理员")
    if db.scalar(select(AiRun.id).where(AiRun.owner_id == owner, AiRun.status.in_(ACTIVE)).limit(1)):
        raise AppError(409, "run_active", "已有回复正在生成，请等待或先取消")
    history = runs(db, owner, conversation_id)
    latest = history[-1].id if history else None
    if data.previous_run_id != latest:
        raise AppError(409, "conversation_changed", "对话已有新消息，请刷新后再发送")
    if len(history) >= 100:
        raise AppError(422, "conversation_full", "这段对话已达到 100 轮，请新建对话")
    recent = db.scalar(select(func.count()).select_from(AiRun).where(AiRun.owner_id == owner, AiRun.created_at > utcnow() - timedelta(hours=1)))
    if recent >= 30:
        raise AppError(429, "ai_rate_limited", "每小时最多发送 30 次，请稍后再试")
    from app.modules.ai import image_context, pdf_context, source_context
    source = image_context.freeze(db, owner, data.image) if data.image else pdf_context.freeze(db, owner, data.pdf) if data.pdf else None
    if source and data.model != "deepseek-flash":
        raise AppError(422, "vision_model_required", "图片和 PDF 页图提问需要支持图片的 DeepSeek Flash")
    for item in history:
        if item.status == "succeeded" and item.source:
            source_context.validate(db, owner, item.source)
            if source and (source["note_id"] != item.source["note_id"] or source_context.is_image(source) != source_context.is_image(item.source)):
                raise AppError(422, "pdf_conversation_scope", "一段对话仅引用同一本同类型来源，更换资料请新建对话")
    messages = [{"role": "system", "content": SYSTEM}]
    for item in history:
        # Failed/cancelled turns are visible in history but excluded from future model context.
        if item.status == "succeeded":
            scope = ""
            if item.source:
                label = "图片页码" if source_context.is_image(item.source) else "原 PDF 页码"
                scope = "\n[当时附加" + label + "：" + ",".join(str(p["number"]) for p in item.source["pages"])
                if source_context.is_image(item.source):
                    scope += "；" + source_context.region_text(item.source["pages"][0].get("region"))
                scope += "]"
            messages.extend([{"role": "user", "content": item.prompt + scope}, {"role": "assistant", "content": item.answer}])
    messages.append({"role": "user", "content": data.prompt})
    if sum(len(m["content"].encode("utf-8")) for m in messages) > 32000:
        raise AppError(422, "context_too_large", "这段对话的上下文已满，请新建对话；不会自动截掉前文")
    if not history:
        parent.title = data.prompt.strip()[:64]
    row = AiRun(owner_id=owner, conversation_id=conversation_id, request_key=data.request_key,
                request_hash=request_hash, prompt=data.prompt, model=data.model, input_messages=messages, source=source)
    if source:
        groups = pdf_context.batches(source)
        row.progress = {"stage": "queued", "completed_pages": 0, "total_pages": len(source["pages"]),
                        "completed_batches": 0, "total_batches": len(groups), "call_count": 0,
                        "planned_calls": pdf_context.planned_calls(len(groups)), "current_pages": [], "usage_complete": False}
    db.add(row)
    db.commit()
    return row


def recover(db):
    # A lost lease may have already incurred a charge. Never put it back in the queue.
    db.execute(update(AiRun).where(AiRun.status == "running", AiRun.lease_until < utcnow()).values(
        status="interrupted", error_code="worker_interrupted", finished_at=utcnow(), lease_until=None))
    db.execute(update(AiStep).where(AiStep.status == "running", AiStep.run_id.in_(
        select(AiRun.id).where(AiRun.status.not_in(ACTIVE)))).values(status="failed", error_code="run_interrupted"))


def claim():
    with SessionLocal.begin() as db:
        recover(db)
        row = db.scalar(select(AiRun).where(AiRun.status == "queued").order_by(AiRun.created_at)
                        .with_for_update(skip_locked=True).limit(1))
        if not row:
            return None
        row.status = "running"
        row.lease_until = utcnow() + timedelta(seconds=60)
        # Commit before the external POST, including on a later ambiguous provider failure.
        return row.id


def save_progress(run_id, answer, usage, terminal=None, code=None):
    with SessionLocal.begin() as db:
        # Always lock the owner before the run, matching authenticated mutations.
        owner = db.scalar(select(AiRun.owner_id).where(AiRun.id == run_id))
        user = db.scalar(select(User).where(User.id == owner).with_for_update())
        row = db.scalar(select(AiRun).where(AiRun.id == run_id).with_for_update())
        if not row or row.status != "running" or row.lease_until < utcnow():
            return False
        if user.status != "active":
            terminal, code = "cancelled", "account_disabled"
        row.answer = answer
        row.usage = usage
        row.lease_until = utcnow() + timedelta(seconds=60)
        if terminal:
            row.status, row.error_code = terminal, code
            row.finished_at, row.lease_until = utcnow(), None
        return terminal is None


def execute(run_id):
    answer, usage, finish = "", None, None
    last_saved = 0.0
    try:
        with SessionLocal() as db:
            row = db.get(AiRun, run_id)
            model, messages, source = row.model, row.input_messages, row.source
        if source:
            from app.modules.ai.pdf_chat import execute_pdf
            execute_pdf(run_id)
            return
        if not save_progress(run_id, answer, usage):
            return
        secret = settings().deepseek_api_key.get_secret_value()
        if not secret:
            raise chat_provider.ProviderFailure("ai_not_configured")
        stream = chat_provider.stream_chat(secret, model, messages)
        try:
            for event in stream:
                answer += event.get("delta", "")
                if len(answer) > 64000:
                    raise chat_provider.ProviderFailure("provider_invalid_response")
                usage = event.get("usage", usage)
                finish = event.get("finish", finish)
                if time.monotonic() - last_saved >= 0.25:
                    if not save_progress(run_id, answer, usage):
                        return
                    last_saved = time.monotonic()
        finally:
            stream.close()  # Closes the upstream connection on cancel/error as well.
        if not finish or not answer:
            raise chat_provider.ProviderFailure("provider_incomplete")
        save_progress(run_id, answer, usage, "truncated" if finish == "length" else "succeeded",
                      "output_limit" if finish == "length" else None)
    except chat_provider.ProviderFailure as error:
        save_progress(run_id, answer, usage, "interrupted" if answer else "failed", error.code)
    except Exception:
        # Do not log prompts, raw provider bodies or credentials, and never retry a POST.
        try:
            save_progress(run_id, answer, usage, "interrupted", "worker_interrupted")
        except Exception:
            pass  # Lease expiry will expose the interruption after database recovery.


def tick():
    run_id = claim()
    if run_id:
        execute(run_id)
    return bool(run_id)


def snapshot(owner, run_id, auth_session_id):
    with SessionLocal() as db:
        session = db.get(AuthSession, auth_session_id)
        user = db.get(User, owner)
        if (not session or session.user_id != owner or session.revoked_at or session.expires_at <= utcnow()
                or not user or user.status != "active"):
            raise AppError(401, "login_required", "会话已失效，请重新登录")
        return RunOut.model_validate(get_run(db, owner, run_id))
