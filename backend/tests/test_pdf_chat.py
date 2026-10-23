from datetime import timedelta
import base64
import hashlib
import io
import json
from uuid import UUID, uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.modules.ai import chat_provider, chat_service, pdf_context
from app.modules.ai.models import AiRun, AiStep
from app.modules.files import service as files
from app.modules.files.models import File, FileVariant, NotePage, PdfImport
from app.modules.notes import service as notes
from app.modules.notes.models import Note
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.models import User
from app.shared.security import hasher
from conftest import login, mutate
from test_ai_chat import conversation, stream_bytes


@pytest.fixture(autouse=True)
def private_config(monkeypatch,tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY","sk-fake-pdf-chat-tests-not-a-real-key")
    settings.cache_clear()
    monkeypatch.setattr(settings(),"storage_root",str(tmp_path/"private"))
    def no_live():
        raise AssertionError("Regression tests must not call the real model")
    monkeypatch.setattr(chat_provider,"create_client",no_live)
    yield
    settings.cache_clear()


def pdf_book(client, count=3):
    response=mutate(client,"POST","/notes",{"title":"PDF context fixture","kind":"image"})
    assert response.status_code==201,response.text
    note_id=UUID(response.json()["id"])
    with SessionLocal.begin() as db:
        note=db.get(Note,note_id)
        source=File(owner_id=note.owner_id,object_key=f"{note.owner_id}/{uuid4()}/source.pdf",filename="test.pdf",mime="application/pdf",expected_bytes=20,bytes=20,status="ready")
        db.add(source);db.flush()
        db.add(PdfImport(owner_id=note.owner_id,note_id=note.id,source_file_id=source.id,request_hash="0"*64,status="ready",page_count=count,completed_pages=count))
        for number in range(1,count+1):
            image=Image.new("RGB",(40,60),(number%255,number*3%255,number*7%255))
            buffer=io.BytesIO();image.save(buffer,format="JPEG");data=buffer.getvalue()
            fid=uuid4();key=f"{note.owner_id}/{fid}/reading.jpg"
            files.storage().put(key,data)
            db.add(File(id=fid,owner_id=note.owner_id,object_key=key+".original",filename=f"page-{number}.jpg",mime="image/jpeg",expected_bytes=len(data),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),status="ready",width=40,height=60))
            db.flush()
            db.add(FileVariant(file_id=fid,kind="reading",object_key=key,bytes=len(data)))
            db.add(NotePage(owner_id=note.owner_id,note_id=note.id,file_id=fid,position=number-1,pdf_page_number=number))
        db.flush();notes.revise(db,note,note.revision,{})
        notes.save_snapshot(db,note)
    return str(note_id)


def source(client,note_id,mode="all",numbers=None):
    detail=client.get("/api/v1/ai/pdf-notebooks/"+note_id)
    assert detail.status_code==200,detail.text
    return {"note_id":note_id,"revision":detail.json()["revision"],"mode":mode,"page_numbers":numbers or []}


def submit(client,note_id,**changes):
    cid=changes.pop("conversation_id",None) or conversation(client)
    pdf=changes.pop("pdf",None) or source(client,note_id)
    body={"request_key":str(uuid4()),"prompt":"总结资料并引用原PDF页码","pdf":pdf,**changes}
    return mutate(client,"POST",f"/ai/conversations/{cid}/runs",body)


def saved(client,row):
    return client.get("/api/v1/ai/runs/"+row["id"]).json()


def mock(monkeypatch,handler=None):
    calls=[]
    def handle(request):
        calls.append(json.loads(request.content))
        return handler(request,len(calls)) if handler else httpx.Response(200,content=stream_bytes("资料内容[[p:1]]"))
    monkeypatch.setattr(chat_provider,"create_client",lambda:httpx.Client(transport=httpx.MockTransport(handle)))
    return calls


def test_selection_actual_images_frozen_order_and_safe_source_links(client,monkeypatch):
    login(client);nid=pdf_book(client)
    selected=source(client,nid,"pages",[3,1])
    preview=mutate(client,"POST","/ai/pdf-preview",selected)
    assert preview.status_code==200 and preview.json()["batches"]==[[1,3]]
    cid,key=conversation(client),str(uuid4())
    row=submit(client,nid,pdf=selected,conversation_id=cid,request_key=key).json()
    assert submit(client,nid,pdf=selected,conversation_id=cid,request_key=key).json()["id"]==row["id"]
    assert submit(client,nid,pdf=source(client,nid,"pages",[1]),conversation_id=cid,request_key=key).status_code==409
    assert "object_key" not in json.dumps(row) and "file_id" not in json.dumps(row)
    calls=mock(monkeypatch)
    assert chat_service.tick() and not chat_service.tick()
    result=saved(client,row)
    assert result["status"]=="succeeded" and result["progress"]["completed_pages"]==2
    assert result["progress"]["call_count"]==1 and result["progress"]["usage_complete"]
    content=calls[0]["messages"][-1]["content"]
    labels=[p["text"] for p in content if p["type"]=="text"]
    assert labels[-2:]==["以下为原PDF第1页：","以下为原PDF第3页："]
    images=[base64.b64decode(p["image_url"]["url"].split(",",1)[1]) for p in content if p["type"]=="image_url"]
    with SessionLocal() as db:
        frozen=db.get(AiRun,UUID(row["id"])).source
    assert images==[files.storage().read(p["object_key"]) for p in frozen["pages"]]
    assert len(calls)==1
    assert submit(client,nid,pdf=selected,conversation_id=cid,request_key=key).json()["id"]==row["id"]
    assert not chat_service.tick() and len(calls)==1
    page=row["source"]["pages"][0]
    link=f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}"
    assert client.get(link).json()["page_number"]==1
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{uuid4()}").status_code==404
    events=client.get(f"/api/v1/ai/runs/{row['id']}/events")
    assert "object_key" not in events.text and "event: done" in events.text


def test_entire_pdf_two_batches_and_final_aggregates_usage_without_skipping_pages(client,monkeypatch):
    login(client);nid=pdf_book(client,9)
    row=submit(client,nid).json();calls=mock(monkeypatch)
    chat_service.tick();result=saved(client,row)
    assert result["status"]=="succeeded"
    assert result["progress"]=={"stage":"done","completed_pages":9,"total_pages":9,"completed_batches":2,"total_batches":2,"call_count":3,"planned_calls":3,"current_pages":list(range(1,10)),"usage_complete":True}
    assert len(calls)==3
    counts=[sum(p["type"]=="image_url" for p in c["messages"][-1]["content"]) if isinstance(c["messages"][-1]["content"],list) else 0 for c in calls]
    assert counts==[8,1,0] and result["usage"]["total_tokens"]==54
    assert "原PDF页码" in calls[-1]["messages"][-1]["content"]
    with SessionLocal() as db:
        steps=list(db.scalars(select(AiStep).where(AiStep.run_id==UUID(row["id"])).order_by(AiStep.position)))
        assert [s.kind for s in steps]==["read","read","answer"]
        assert all(s.status=="succeeded" for s in steps)


def test_two_hundred_pages_use_bounded_hierarchical_summary(client,monkeypatch):
    login(client);nid=pdf_book(client,200)
    row=submit(client,nid).json();calls=mock(monkeypatch)
    chat_service.tick();result=saved(client,row)
    assert result["status"]=="succeeded" and result["progress"]["completed_pages"]==200
    assert len(calls)==pdf_context.planned_calls(25)==34
    with SessionLocal() as db:
        steps=list(db.scalars(select(AiStep).where(AiStep.run_id==UUID(row["id"]))))
        assert sum(s.kind=="read" for s in steps)==25
        assert sum(s.kind=="reduce" for s in steps)==8
        assert sorted(n for s in steps if s.kind=="read" for n in s.pages)==list(range(1,201))


def test_later_batch_failure_keeps_completed_work_and_never_automatically_retries(client,monkeypatch):
    login(client);nid=pdf_book(client,9)
    row=submit(client,nid).json()
    calls=mock(monkeypatch,lambda _,n:httpx.Response(200,content=stream_bytes()) if n==1 else httpx.Response(500,text="private provider error"))
    chat_service.tick();assert not chat_service.tick()
    result=saved(client,row)
    assert result["status"]=="interrupted" and result["progress"]["completed_pages"]==8
    assert result["progress"]["current_pages"]==[9] and not result["progress"]["usage_complete"]
    assert result["usage"]["total_tokens"]==18 and len(calls)==2
    with SessionLocal() as db:
        steps=list(db.scalars(select(AiStep).where(AiStep.run_id==UUID(row["id"])).order_by(AiStep.position)))
        assert steps[0].answer and steps[1].status=="failed"


def test_cancel_between_batches_prevents_any_remaining_calls(client,monkeypatch):
    login(client);nid=pdf_book(client,9)
    row=submit(client,nid).json();calls=[]
    def fake(*args,**kwargs):
        calls.append(True)
        yield {"delta":"第一批已读[[p:1]]"}
        yield {"finish":"stop","usage":{"total_tokens":2,"prompt_tokens":1,"completion_tokens":1}}
        mutate(client,"POST",f"/ai/runs/{row['id']}/cancel",{})
    monkeypatch.setattr(chat_provider,"stream_chat",fake)
    chat_service.tick();result=saved(client,row)
    assert result["status"]=="cancelled" and len(calls)==1
    assert result["progress"]["completed_pages"]==0  # Cancellation won the checkpoint race.
    assert not chat_service.tick()


@pytest.mark.parametrize("reason,code",[("length","pdf_summary_truncated"),("aborted","provider_incomplete")])
def test_incomplete_summary_never_becomes_a_full_book_answer(client,monkeypatch,reason,code):
    login(client);nid=pdf_book(client,9)
    row=submit(client,nid).json();calls=mock(monkeypatch,lambda *_:httpx.Response(200,content=stream_bytes(reason=reason)))
    chat_service.tick();result=saved(client,row)
    assert result["status"]!="succeeded" and result["error_code"]==code and len(calls)==1
    assert result["progress"]["completed_pages"]==0


def test_owners_admin_capabilities_and_private_read_preview_paths(client):
    login(client);nid=pdf_book(client);row=submit(client,nid).json()
    with SessionLocal.begin() as db:
        db.add(User(username="alice",display_name="Alice",password_hash=hasher.hash("member-password")))
    login(client,"alice","member-password")
    assert client.get("/api/v1/ai/pdf-notebooks").json()==[]
    assert client.get("/api/v1/ai/pdf-notebooks/"+nid).status_code==404
    assert mutate(client,"POST","/ai/pdf-preview",{"note_id":nid,"revision":2,"mode":"all"}).status_code==404
    assert submit(client,nid,pdf={"note_id":nid,"revision":2,"mode":"all"}).status_code==404
    page=row["source"]["pages"][0]
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}").status_code==404
    login(client)
    mutate(client,"POST",f"/ai/runs/{row['id']}/cancel",{})
    assert submit(client,nid,model="deepseek-v4-pro").json()["code"]=="vision_model_required"


def test_original_numbering_survives_reorder_and_ignores_later_images(client,monkeypatch):
    login(client);nid=pdf_book(client)
    original=client.get("/api/v1/ai/pdf-notebooks/"+nid).json()
    pages=client.get(f"/api/v1/notes/{nid}/pages").json()
    response=mutate(client,"PATCH",f"/notes/{nid}/pages/order",{"revision":original["revision"],"page_ids":[p["id"] for p in reversed(pages)]})
    assert response.status_code==200,response.text
    current=client.get("/api/v1/ai/pdf-notebooks/"+nid).json()
    assert [p["page_id"] for p in current["pages"]]==[p["page_id"] for p in original["pages"]]
    with SessionLocal.begin() as db:
        note=db.get(Note,UUID(nid));fid=uuid4()
        db.add(File(id=fid,owner_id=note.owner_id,object_key=f"{note.owner_id}/{fid}/fake.jpg",filename="第0004页.jpg",mime="image/jpeg",expected_bytes=1,status="ready"));db.flush()
        db.add(NotePage(owner_id=note.owner_id,note_id=note.id,file_id=fid,position=3))
    assert len(client.get("/api/v1/ai/pdf-notebooks/"+nid).json()["pages"])==3
    row=submit(client,nid).json();mock(monkeypatch);chat_service.tick()
    with SessionLocal.begin() as db:
        note=db.get(Note,UUID(nid));note.revision+=1
    page=row["source"]["pages"][0]
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}").json()["source_changed"]


def test_source_deleted_before_or_between_calls_and_old_history_cannot_be_forwarded(client,monkeypatch):
    login(client);nid=pdf_book(client,9)
    row=submit(client,nid).json()
    def handle(_,n):
        if n==1:
            with SessionLocal.begin() as db:db.get(Note,UUID(nid)).deleted_at=utcnow()
        return httpx.Response(200,content=stream_bytes())
    calls=mock(monkeypatch,handle);chat_service.tick()
    assert len(calls)==1 and saved(client,row)["error_code"]=="pdf_source_unavailable"
    assert client.get("/api/v1/ai/pdf-notebooks").json()==[]
    page=row["source"]["pages"][0]
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}").status_code==404


@pytest.mark.parametrize("numbers",[[],[0],[4],[True],["1"],[1,1]])
def test_invalid_pages_do_not_dispatch(client,numbers):
    login(client);nid=pdf_book(client)
    response=submit(client,nid,pdf=source(client,nid,"pages",numbers))
    assert response.status_code==422,response.text


def test_preview_stale_revision_size_and_missing_mapping_fail_before_payment(client,monkeypatch):
    login(client);nid=pdf_book(client)
    data=source(client,nid);data["revision"]-=1
    assert mutate(client,"POST","/ai/pdf-preview",data).status_code==409
    monkeypatch.setattr(pdf_context,"TOTAL_BYTES",1)
    assert mutate(client,"POST","/ai/pdf-preview",source(client,nid)).json()["code"]=="pdf_context_too_large"
    with SessionLocal.begin() as db:
        page=db.scalar(select(NotePage).where(NotePage.note_id==UUID(nid)));page.pdf_page_number=None
    assert client.get("/api/v1/ai/pdf-notebooks/"+nid).json()["code"]=="pdf_pages_unavailable"


def test_crash_after_paid_step_never_restarts_or_overwrites_it(client,monkeypatch):
    login(client);nid=pdf_book(client)
    row=submit(client,nid).json();rid=chat_service.claim()
    from app.modules.ai.pdf_chat import start_step,save_step
    sid=start_step(rid,"answer",[1,2,3])
    assert save_step(rid,sid,"已保存的部分",None)
    with SessionLocal.begin() as db:db.get(AiRun,rid).lease_until=utcnow()-timedelta(seconds=1)
    assert not chat_service.tick()
    assert not save_step(rid,sid,"旧写入",None,done=True)
    assert saved(client,row)["answer"]=="已保存的部分"
    with SessionLocal() as db:assert db.get(AiStep,sid).status=="failed"


def test_legacy_pdf_number_backfill_uses_pre_reorder_snapshot(client):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.shared.db import engine
    login(client);nid=pdf_book(client)
    with SessionLocal.begin() as db:
        ordered=list(db.scalars(select(NotePage).where(NotePage.note_id==UUID(nid)).order_by(NotePage.position)))
        original={p.id:p.pdf_page_number for p in ordered}
        # Move to temporary positions first, matching real reorder's uniqueness rules.
        for p in ordered:p.position+=10;p.pdf_page_number=None
        db.flush()
        for index,p in enumerate(reversed(ordered)):p.position=index
        note=db.get(Note,UUID(nid));note.revision+=1;db.flush();notes.save_snapshot(db,note)
    path=Path('alembic/versions/63b9d8343e02_pdf_sources_and_durable_ai_batch_steps.py')
    spec=importlib.util.spec_from_file_location('pdf_context_migration',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.backfill_pdf_pages()
    with SessionLocal() as db:
        assert {p.id:p.pdf_page_number for p in db.scalars(select(NotePage).where(NotePage.note_id==UUID(nid)))}==original


def test_nonadmin_pdf_run_succeeds_and_admin_cannot_read_its_source(client,monkeypatch):
    login(client)
    with SessionLocal.begin() as db:db.add(User(username="alice",display_name="Alice",password_hash=hasher.hash("member-password")))
    login(client,"alice","member-password");nid=pdf_book(client)
    row=submit(client,nid).json();calls=mock(monkeypatch);chat_service.tick()
    assert saved(client,row)["status"]=="succeeded" and len(calls)==1
    login(client)
    assert client.get("/api/v1/ai/pdf-notebooks/"+nid).status_code==404
    page=row["source"]["pages"][0]
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}").status_code==404


def test_byte_budget_creates_smaller_batches_and_preview_is_readonly(client):
    login(client);nid=pdf_book(client)
    with SessionLocal.begin() as db:
        for variant in db.scalars(select(FileVariant).join(NotePage,NotePage.file_id==FileVariant.file_id).where(NotePage.note_id==UUID(nid))):
            variant.bytes=7*1024*1024
    preview=mutate(client,"POST","/ai/pdf-preview",source(client,nid))
    assert preview.json()["batches"]==[[1],[2],[3]] and preview.json()["planned_calls"]==4
    with SessionLocal() as db:assert not list(db.scalars(select(AiRun))) and not list(db.scalars(select(AiStep)))


def test_completed_pdf_history_is_rechecked_before_forwarding_to_model(client,monkeypatch):
    login(client);nid=pdf_book(client)
    row=submit(client,nid).json();calls=mock(monkeypatch);chat_service.tick()
    with SessionLocal.begin() as db:db.get(Note,UUID(nid)).deleted_at=utcnow()
    response=mutate(client,"POST",f"/ai/conversations/{row['conversation_id']}/runs",{
        "request_key":str(uuid4()),"previous_run_id":row['id'],"prompt":"继续解释"})
    assert response.status_code==404 and len(calls)==1


def test_storage_corruption_or_disable_before_send_never_calls_model(client,monkeypatch):
    login(client);nid=pdf_book(client);row=submit(client,nid).json()
    with SessionLocal() as db:key=db.get(AiRun,UUID(row["id"])).source["pages"][0]["object_key"]
    files.storage().put(key,b"corrupt")
    chat_service.tick();assert saved(client,row)["error_code"]=="pdf_source_unavailable"
    row=submit(client,nid).json()
    with SessionLocal.begin() as db:db.scalar(select(User).where(User.username=="admin")).status="disabled"
    chat_service.tick()
    with SessionLocal() as db:assert db.get(AiRun,UUID(row["id"])).status=="cancelled"
