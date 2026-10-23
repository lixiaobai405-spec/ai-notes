"""Render one page at a time in a disposable process, with a deadline per operation."""
import math
import multiprocessing
from pathlib import Path
import tempfile


class InvalidPdf(ValueError):
    pass


class PdfRenderFailure(RuntimeError):
    pass


def _render_child(connection, source, directory, max_pages, max_bytes, max_pixels):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        with pdfium.PdfDocument(source) as document:
            count = len(document)
            if not 1 <= count <= max_pages:
                connection.send(("error", "pdf_page_limit"))
                return
            connection.send(("info", count))
            while True:
                index = connection.recv()
                if index is None:
                    return
                page = document[index]
                try:
                    width, height = page.get_size()
                    if (not all(math.isfinite(v) and 0 < v <= 14400 for v in (width, height))
                            or max(width, height) / min(width, height) > 20):
                        connection.send(("error", "pdf_page_dimensions"))
                        return
                    scale = min(200 / 72, 3200 / max(width, height))
                    if math.ceil(width * scale) * math.ceil(height * scale) > max_pixels:
                        connection.send(("error", "pdf_page_pixels"))
                        return
                    target = Path(directory) / f"page-{index:05d}.jpg"
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        try:
                            image.save(target, format="JPEG", quality=94, optimize=True)
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                    if target.stat().st_size > max_bytes:
                        connection.send(("error", "pdf_page_bytes"))
                        return
                    connection.send(("page", index))
                finally:
                    page.close()
    except pdfium.PdfiumError as error:
        code = "pdf_password_required" if error.err_code == raw.FPDF_ERR_PASSWORD else "pdf_invalid"
        connection.send(("error", code))
    except (EOFError, BrokenPipeError):
        pass
    except OSError:
        connection.send(("failure", "pdf_renderer_failed"))
    except Exception:
        connection.send(("error", "pdf_invalid"))
    finally:
        connection.close()


def render_pdf(data, start, *, max_pages, timeout, max_bytes, max_pixels):
    # PDFium is not thread-safe. Spawn also avoids inheriting database connections.
    with tempfile.TemporaryDirectory(prefix="ai-notes-pdf-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(data)
        parent, child = multiprocessing.get_context("spawn").Pipe()
        process = multiprocessing.get_context("spawn").Process(
            target=_render_child, args=(child, str(source), directory, max_pages, max_bytes, max_pixels), daemon=True)
        process.start()
        child.close()

        def receive():
            if not parent.poll(timeout):
                raise PdfRenderFailure("pdf_render_timeout")
            try:
                message = parent.recv()
            except EOFError:
                raise PdfRenderFailure("pdf_renderer_failed") from None
            if message[0] == "error":
                raise InvalidPdf(message[1])
            if message[0] == "failure":
                raise PdfRenderFailure(message[1])
            return message

        try:
            message = receive()
            if message[0] != "info":
                raise PdfRenderFailure("pdf_renderer_failed")
            count = message[1]
            yield {"page_count": count}
            for index in range(start, count):
                parent.send(index)
                if receive() != ("page", index):
                    raise PdfRenderFailure("pdf_renderer_failed")
                yield {"index": index, "data": (Path(directory) / f"page-{index:05d}.jpg").read_bytes()}
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            process.close()
