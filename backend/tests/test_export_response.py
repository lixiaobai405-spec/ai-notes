"""The private temporary archive is cleaned up even if the transport disappears."""

import asyncio
import io

import pytest
from starlette.requests import ClientDisconnect

from app.modules.exports.router import ArchiveResponse, _downloads
from app.shared.config import Settings


@pytest.fixture(autouse=True)
def clean_database():
    yield


@pytest.mark.parametrize("disconnect", [False, True])
def test_download_releases_temporary_file_and_slot(disconnect):
    archive = io.BytesIO(b"archive bytes")
    assert _downloads.acquire(blocking=False)
    response = ArchiveResponse(archive, "ai-notes-export.zip")
    frames = []

    async def receive():
        await asyncio.Event().wait()

    async def send(frame):
        assert not _downloads.acquire(blocking=False)
        if disconnect and frame["type"] == "http.response.body":
            raise OSError("connection closed")
        frames.append(frame)

    async def run():
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    if disconnect:
        with pytest.raises(ClientDisconnect):
            asyncio.run(run())
    else:
        asyncio.run(run())
        assert b"".join(frame.get("body", b"") for frame in frames) == b"archive bytes"
    assert archive.closed
    assert _downloads.acquire(blocking=False)
    _downloads.release()


def test_close_failure_still_releases_download_slot():
    class BrokenClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("close failed")

    archive = BrokenClose(b"archive bytes")
    assert _downloads.acquire(blocking=False)
    response = ArchiveResponse(archive, "ai-notes-export.zip")

    async def receive():
        await asyncio.Event().wait()

    async def send(_frame):
        pass

    async def run():
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    with pytest.raises(OSError, match="close failed"):
        asyncio.run(run())
    assert archive.closed
    assert _downloads.acquire(blocking=False)
    _downloads.release()


@pytest.mark.parametrize("field,value", [
    ("export_max_bytes", 0), ("export_max_bytes", 512 * 1024 * 1024 + 1),
    ("export_max_rows", 0), ("export_max_rows", 50_001),
    ("export_max_metadata_bytes", 0), ("export_max_metadata_bytes", 32 * 1024 * 1024 + 1),
    ("export_max_seconds", 0),
])
def test_export_settings_stay_within_the_browser_and_format_limits(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_task_cancellation_releases_temporary_file_and_slot():
    archive = io.BytesIO(b"archive bytes")
    assert _downloads.acquire(blocking=False)
    response = ArchiveResponse(archive, "ai-notes-export.zip")

    async def receive():
        await asyncio.Event().wait()

    async def send(frame):
        if frame["type"] == "http.response.body":
            raise asyncio.CancelledError()

    async def run():
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert archive.closed
    assert _downloads.acquire(blocking=False)
    _downloads.release()


def test_receive_disconnect_on_asgi_23_releases_download():
    archive = io.BytesIO(b"a" * (1024 * 1024))
    assert _downloads.acquire(blocking=False)
    response = ArchiveResponse(archive, "ai-notes-export.zip")

    async def run():
        first_chunk = asyncio.Event()

        async def receive():
            await first_chunk.wait()
            return {"type": "http.disconnect"}

        async def send(frame):
            if frame["type"] == "http.response.body":
                first_chunk.set()
                await asyncio.sleep(0)

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), timeout=3)

    asyncio.run(run())
    assert archive.closed
    assert _downloads.acquire(blocking=False)
    _downloads.release()
