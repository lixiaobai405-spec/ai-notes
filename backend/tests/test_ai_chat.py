from datetime import timedelta
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.modules.ai import chat_provider as provider, chat_service as service
from app.modules.ai.models import AiConversation, AiRun
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.models import AuthSession, User
from app.shared.security import hasher
from conftest import login, mutate

KEY = "sk-fake-chat-tests-only-never-a-real-key"


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    settings.cache_clear()
    def no_live():
        raise AssertionError("Live model requests are forbidden in regression tests")
    monkeypatch.setattr(provider, "create_client", no_live)
    yield
    settings.cache_clear()


def conversation(client):
    response = mutate(client, "POST", "/ai/conversations", {"request_key": str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def submit(client, cid, prompt="你好", **kwargs):
    return mutate(client, "POST", f"/ai/conversations/{cid}/runs",
                  {"request_key": str(uuid4()), "prompt": prompt, **kwargs})


def stream_bytes(answer="你好，矩阵🙂", reason="stop", done=True):
    events = [{"choices": [{"delta": {"content": c}, "finish_reason": None}]} for c in answer]
    events += [{"choices": [{"delta": {}, "finish_reason": reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18, "secret": KEY}}]
    return b": keep-alive\n\n" + b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\r\n\r\n" for e in events) + (b"data: [DONE]\n\n" if done else b"")


def mock_http(monkeypatch, response=None, handler=None):
    calls = []
    def handle(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, content=response or stream_bytes())
    monkeypatch.setattr(provider, "create_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return calls


def read(client, run):
    response = client.get("/api/v1/ai/runs/" + run["id"])
    assert response.status_code == 200
    return response.json()


def test_real_adapter_context_history_sse_and_duplicate_are_single_attempt(client, monkeypatch):
    login(client)
    cid = conversation(client)
    key = str(uuid4())
    first = submit(client, cid, request_key=key).json()
    assert submit(client, cid, request_key=key).json()["id"] == first["id"]
    assert submit(client, cid, "不同问题", request_key=key).status_code == 409
    calls = mock_http(monkeypatch)
    assert service.tick()
    assert not service.tick()
    saved = read(client, first)
    assert saved["answer"] == "你好，矩阵🙂" and saved["status"] == "succeeded"
    assert saved["usage"] == {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == provider.URL
    assert request.headers["Authorization"] == "Bearer " + KEY
    payload = json.loads(request.content)
    assert payload["thinking"] == {"type": "disabled"} and payload["max_tokens"] == 4096
    assert payload["messages"][-1] == {"role": "user", "content": "你好"}
    detail = client.get(f"/api/v1/ai/conversations/{cid}").json()
    assert detail["title"] == "你好" and detail["runs"][0] == saved
    events = client.get(f"/api/v1/ai/runs/{first['id']}/events")
    assert "text/event-stream" in events.headers["content-type"]
    assert "event: meta" in events.text and "event: done" in events.text and KEY not in events.text
    assert len(calls) == 1  # Reconnecting/refreshing only reads saved state.
    second = submit(client, cid, "再解释一次", previous_run_id=first["id"])
    assert second.status_code == 201
    with SessionLocal() as db:
        messages = db.get(AiRun, UUID(second.json()["id"])).input_messages
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[-2]["content"] == saved["answer"]
    # A completed request key still resolves to the original result, not another generation.
    assert submit(client, cid, request_key=key).json()["id"] == first["id"]


def test_auth_csrf_origin_and_owner_isolation_including_admin(client):
    assert client.get("/api/v1/ai/conversations").status_code == 401
    login(client)
    assert client.post("/api/v1/ai/conversations", json={"request_key":str(uuid4())}).status_code == 403
    assert client.post("/api/v1/ai/conversations", headers={"Origin":"http://testserver"}, json={"request_key":str(uuid4())}).status_code == 403
    with SessionLocal.begin() as db:
        db.add(User(username="alice", display_name="Alice", password_hash=hasher.hash("member-password")))
    login(client, "alice", "member-password")
    cid = conversation(client)
    run = submit(client, cid).json()
    assert client.get("/api/v1/ai/providers").json()["platform_enabled"]
    login(client)
    assert client.get("/api/v1/ai/conversations").json() == []
    for path in [f"/ai/conversations/{cid}", f"/ai/runs/{run['id']}", f"/ai/runs/{run['id']}/events"]:
        assert client.get("/api/v1"+path).status_code == 404
    assert submit(client, cid).status_code == 404
    assert mutate(client, "POST", f"/ai/runs/{run['id']}/cancel", {}).status_code == 404


def test_conversation_idempotence_stale_history_and_active_guard(client):
    login(client)
    body = {"request_key":str(uuid4())}
    first = mutate(client, "POST", "/ai/conversations", body).json()
    assert mutate(client, "POST", "/ai/conversations", body).json()["id"] == first["id"]
    run = submit(client, first["id"]).json()
    assert submit(client, conversation(client)).json()["code"] == "run_active"
    mutate(client, "POST", f"/ai/runs/{run['id']}/cancel", {})
    assert submit(client, first["id"]).json()["code"] == "conversation_changed"
    assert submit(client, first["id"], previous_run_id=run["id"]).status_code == 201


def test_cancel_before_dispatch_never_calls_provider(client):
    login(client)
    row = submit(client, conversation(client)).json()
    for _ in range(2):
        response = mutate(client, "POST", f"/ai/runs/{row['id']}/cancel", {})
        assert response.json()["status"] == "cancelled"
    assert not service.tick()


def test_cancel_while_streaming_preserves_saved_partial_and_closes_provider(client, monkeypatch):
    login(client)
    row = submit(client, conversation(client)).json()
    closed = []
    def fake(*_):
        try:
            yield {"delta":"已保存的内容"}
            mutate(client, "POST", f"/ai/runs/{row['id']}/cancel", {})
            yield {"delta":"不应覆盖取消状态"}
        finally:
            closed.append(True)
    monkeypatch.setattr(provider, "stream_chat", fake)
    service.tick()
    saved = read(client, row)
    assert saved["status"] == "cancelled" and saved["answer"] == "已保存的内容"
    assert closed == [True]


def test_provider_failure_after_partial_keeps_answer_and_never_retries(client, monkeypatch):
    login(client)
    row = submit(client, conversation(client)).json()
    def fake(*_):
        yield {"delta":"部分回答"}
        raise provider.ProviderFailure("provider_timeout")
    monkeypatch.setattr(provider, "stream_chat", fake)
    assert service.tick() and not service.tick()
    saved = read(client, row)
    assert saved["status"] == "interrupted" and saved["answer"] == "部分回答"
    assert saved["error_code"] == "provider_timeout"


def test_crashed_worker_and_stale_writer_cannot_restart_or_overwrite(client):
    login(client)
    row = submit(client, conversation(client)).json()
    run_id = service.claim()
    with SessionLocal.begin() as db:
        item = db.get(AiRun, run_id)
        item.answer = "重启前保存的内容"
        item.lease_until = utcnow() - timedelta(seconds=1)
    assert not service.tick()
    assert not service.save_progress(run_id, "旧进程的内容", None, "succeeded")
    saved = read(client, row)
    assert saved["status"] == "interrupted" and saved["answer"] == "重启前保存的内容"


@pytest.mark.parametrize("status,code",[(401,"credential_rejected"),(402,"provider_balance"),(429,"provider_rate_limited"),(500,"provider_unavailable"),(302,"provider_unavailable")])
def test_provider_http_errors_safe_and_single_attempt(client, monkeypatch, caplog, status, code):
    login(client)
    row = submit(client, conversation(client)).json()
    calls = mock_http(monkeypatch, handler=lambda _:httpx.Response(status,text=KEY,headers={"Location":"http://localhost"}))
    service.tick()
    saved = read(client, row)
    assert saved["status"] == "failed" and saved["error_code"] == code
    assert len(calls) == 1 and KEY not in json.dumps(saved) and KEY not in caplog.text


@pytest.mark.parametrize("raw",[b"data: invalid\n\n",b"data: [DONE]\n\n",b"data: "+b"x"*70000,stream_bytes(done=False),b'data: {"error":{"message":"private"}}\n\n'], ids=["invalid-json","early-done","oversize-line","missing-done","error-body"])
def test_malformed_or_unfinished_stream_does_not_claim_success(client, monkeypatch, raw):
    login(client)
    row = submit(client, conversation(client)).json()
    mock_http(monkeypatch, raw)
    service.tick()
    assert read(client,row)["status"] in {"failed","interrupted"}


def test_output_length_is_explicitly_truncated(client, monkeypatch):
    login(client)
    row = submit(client, conversation(client)).json()
    mock_http(monkeypatch, stream_bytes(reason="length"))
    service.tick()
    assert read(client,row)["status"] == "truncated"


def test_disabled_account_before_dispatch_stops_call_and_live_read_revocation(client):
    login(client)
    row = submit(client, conversation(client)).json()
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.username=="admin"))
        owner = user.id
        session_id = db.scalar(select(AuthSession.id).where(AuthSession.user_id==owner))
        user.status = "disabled"
    service.tick()  # No mock: any provider call would fail this test.
    with SessionLocal() as db:
        assert db.get(AiRun,UUID(row["id"])).status == "cancelled"
    with pytest.raises(Exception, match="会话已失效"):
        service.snapshot(owner,UUID(row["id"]),session_id)


def test_input_validation_missing_key_and_context_budget(client, monkeypatch):
    login(client)
    cid = conversation(client)
    for prompt in ["  ", "x"*8001, "abc\x00def"]:
        assert submit(client,cid,prompt).status_code == 422
    assert submit(client,cid,model="uncontrolled-model").status_code == 422
    assert submit(client,cid,api_key=KEY).status_code == 422
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    settings.cache_clear()
    assert submit(client,cid).status_code == 503
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    settings.cache_clear()
    row = submit(client,cid).json()
    with SessionLocal.begin() as db:
        item=db.get(AiRun,UUID(row["id"]))
        item.status="succeeded"
        item.answer="中"*11000
    assert submit(client,cid,previous_run_id=row["id"]).json()["code"] == "context_too_large"


def test_hourly_budget(client):
    login(client)
    cid = conversation(client)
    row = submit(client,cid).json()
    with SessionLocal.begin() as db:
        template=db.get(AiRun,UUID(row["id"]))
        template.status="cancelled"
        for _ in range(29):
            db.add(AiRun(owner_id=template.owner_id,conversation_id=template.conversation_id,request_key=uuid4(),
                         request_hash="0"*64,model=template.model,prompt="test",input_messages=[],status="cancelled"))
    latest=client.get(f"/api/v1/ai/conversations/{cid}").json()["runs"][-1]["id"]
    assert submit(client,cid,previous_run_id=latest).json()["code"] == "ai_rate_limited"


def test_live_sse_offsets_and_terminal_snapshot_do_not_dispatch(client, monkeypatch):
    login(client)
    row=submit(client,conversation(client)).json()
    from app.modules.ai.schemas import RunOut
    snapshots=iter([
        RunOut(**{**row,"answer":"首段🙂","status":"running"}),
        RunOut(**{**row,"answer":"首段🙂尾","status":"succeeded"}),
    ])
    monkeypatch.setattr(service,"snapshot",lambda *_:next(snapshots))
    response=client.get(f"/api/v1/ai/runs/{row['id']}/events")
    blocks=response.text.split("\n\n")
    deltas=[json.loads(b.split("data: ",1)[1]) for b in blocks if b.startswith("event: delta")]
    assert deltas==[{"offset":0,"text":"首段🙂"},{"offset":3,"text":"尾"}]
    assert "event: done" in response.text
    assert read(client,row)["status"]=="queued"  # Reading events never starts a generation.


def test_parallel_workers_claim_a_paid_run_only_once(client):
    from concurrent.futures import ThreadPoolExecutor
    login(client)
    row=submit(client,conversation(client)).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed=list(pool.map(lambda _:service.claim(),range(2)))
    assert [str(run) for run in claimed if run]==[row["id"]]
