"""FlowFiles photo API.   Run:  uvicorn api:app --reload

PRD endpoints
    GET    /health
    GET    /operations
    POST   /conversions                 submit one file (async job; wait=true to block)
    POST   /conversions/batch           submit many files -> ZIP result
    GET    /conversions/{job_id}        status + result metadata
    GET    /conversions/{job_id}/result download the output
    DELETE /conversions/{job_id}        delete output early
Convenience endpoints (synchronous, return the image directly)
    POST   /photos/{convert,resize,compress,text,date,name,signature}
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import threading
import time
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response

import operations as ops
import photo_processor as pp
from jobs import JobResult, JobStore

log = logging.getLogger("FlowFiles")
app = FastAPI(title="Photo API", version="0.2.0")

store = JobStore(ttl_seconds=int(os.environ.get("FLOWFILES_RESULT_TTL_S", 900)))
atexit.register(lambda: store.close())
_slots = threading.BoundedSemaphore(int(os.environ.get("FLOWFILES_MAX_CONCURRENT_JOBS", 2)))


# --------------------------------------------------------------------------- #
# Errors — always {"error": "..."}; never internal paths or stack traces
# --------------------------------------------------------------------------- #
@app.exception_handler(pp.ImageError)
async def image_error_handler(_, exc: pp.ImageError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, exc: RequestValidationError):
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first["loc"] if p != "body")
    return JSONResponse(status_code=422,
                        content={"error": f"Invalid or missing field '{field}': {first['msg']}"})


@app.exception_handler(Exception)
async def unexpected_error_handler(_, exc: Exception):
    log.error("Unexpected error: %s", type(exc).__name__)       # type only, never contents
    return JSONResponse(status_code=500, content={"error": "Internal error while processing."})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _read(upload: UploadFile) -> bytes:
    return await upload.read(pp.MAX_UPLOAD_BYTES + 1)


def _parse_options(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise pp.ImageError("options must be valid JSON.", 400)
    if not isinstance(value, dict):
        raise pp.ImageError("options must be a JSON object.", 400)
    return value


async def _execute(work):
    """Run CPU work off the event loop, with a concurrency slot and a hard time limit.

    A thread cannot be killed, so on timeout the client gets 504 immediately and the
    worker stops at its next cooperative ``deadline`` check (compress / batch)."""
    def guarded():
        if not _slots.acquire(timeout=5):
            raise pp.ImageError("The server is busy. Try again shortly.", 503)
        try:
            return work(time.monotonic() + pp.PROCESS_TIMEOUT_S)
        finally:
            _slots.release()

    try:
        return await asyncio.wait_for(run_in_threadpool(guarded), pp.PROCESS_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise pp.ImageError("Processing exceeded the time limit.", 504)


def _single_work(data: bytes, operation: str, options: dict, sig: Optional[bytes], filename: Optional[str]):
    def work(deadline: float) -> JobResult:
        loaded = pp.load_image(data)
        sig_loaded = None
        if sig is not None:
            try:
                sig_loaded = pp.load_image(sig)
            except pp.ImageError as exc:
                raise pp.ImageError("Signature file: " + exc.message, exc.status_code)
        out = ops.apply_operation(loaded, operation, options, sig_loaded, deadline)
        meta = ops.describe_result(loaded, out, operation, filename)
        return JobResult(out.data, "." + out.ext, out.mime, meta["output"]["filename"], meta)
    return work


def _batch_work(items: list, operation: str, options: dict, sig: Optional[bytes]):
    def work(deadline: float) -> JobResult:
        sig_loaded = pp.load_image(sig) if sig is not None else None
        zip_bytes, report = ops.run_batch(items, operation, options, sig_loaded, deadline)
        ok = sum(1 for r in report if r["status"] == "succeeded")
        meta = {"items": report,
                "output": {"filename": "flowfiles_batch.zip", "format": "ZIP", "bytes": len(zip_bytes)},
                "summary": {"total": len(report), "succeeded": ok, "failed": len(report) - ok},
                "warnings": []}
        if ok == 0:
            return JobResult(None, meta=meta, error="None of the files could be processed.")
        return JobResult(zip_bytes, ".zip", "application/zip", "flowfiles_batch.zip", meta)
    return work


def _file_response(result: JobResult) -> Response:
    warnings = "; ".join(result.meta.get("warnings", [])).encode("ascii", "replace").decode()
    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"',
        "X-Output-Bytes": str(len(result.data)),
    }
    if warnings:
        headers["X-Warnings"] = warnings
    if "input" in result.meta:
        headers["X-Original-Bytes"] = str(result.meta["input"]["bytes"])
    return Response(content=result.data, media_type=result.mime, headers=headers)


async def _run_job(job_id: str, work) -> None:
    store.mark_processing(job_id)
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = await _execute(work)
        if result.data is None:
            store.fail(job_id, result.error or "Processing failed.", 422, elapsed(), result.meta)
        else:
            store.complete(job_id, result, elapsed())
    except pp.ImageError as exc:
        store.fail(job_id, exc.message, exc.status_code, elapsed())
    except Exception as exc:                                    # noqa: BLE001
        log.error("Unexpected job error: %s", type(exc).__name__)
        store.fail(job_id, "Internal error while processing.", 500, elapsed())


def _job_response(job_id: str) -> Response:
    job = store.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found or expired."})
    if job.status == "failed":
        return JSONResponse(status_code=job.error_status or 500,
                            content={"error": job.error, "job": job.public()})
    return JSONResponse(status_code=200 if job.status == "succeeded" else 202, content=job.public())


async def _submit(background: BackgroundTasks, job, work, wait: bool) -> Response:
    if wait:
        await _run_job(job.job_id, work)
        return _job_response(job.job_id)
    background.add_task(_run_job, job.job_id, work)     # async callables are awaited by Starlette
    return JSONResponse(status_code=202, content=job.public())


async def _direct(file: UploadFile, operation: str, options: dict,
                  signature: Optional[UploadFile] = None) -> Response:
    spec = ops.require_operation(operation)
    ops.validate_options(operation, options)
    if spec.needs_signature and signature is None:
        raise pp.ImageError("A signature file is required.", 400)
    data = await _read(file)
    sig = await _read(signature) if signature is not None else None
    result = await _execute(_single_work(data, operation, options, sig, file.filename))
    return _file_response(result)


def _drop_none(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


def output_opts(
    output_format: Optional[str] = Form(None),
    quality: Optional[int] = Form(None),
    flatten_background: Optional[str] = Form(None, description="Required when saving a transparent image as JPEG."),
) -> dict:
    return _drop_none(output_format=output_format, quality=quality, flatten_background=flatten_background)


def style_opts(
    position: Optional[str] = Form(None),
    font_size: Optional[int] = Form(None),
    color: Optional[str] = Form(None),
    opacity: Optional[float] = Form(None),
    stroke_width: Optional[int] = Form(None),
    stroke_color: Optional[str] = Form(None),
    background: Optional[str] = Form(None, description="Colour of a box behind the text."),
    background_opacity: Optional[float] = Form(None),
) -> dict:
    return _drop_none(position=position, font_size=font_size, color=color, opacity=opacity,
                      stroke_width=stroke_width, stroke_color=stroke_color,
                      background=background, background_opacity=background_opacity)


# --------------------------------------------------------------------------- #
# PRD endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok", "formats": pp.SUPPORTED_FORMATS}


@app.get("/operations")
def list_operations():
    return ops.describe_operations()


@app.post("/conversions")
async def create_conversion(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    operation: str = Form(...),
    options: str = Form("{}", description="JSON object of operation options (see GET /operations)."),
    signature: Optional[UploadFile] = File(None),
    wait: bool = Form(False, description="Block until the job finishes (fine for photos)."),
):
    opts = _parse_options(options)
    spec = ops.require_operation(operation)
    ops.validate_options(operation, opts)
    if spec.needs_signature and signature is None:
        raise pp.ImageError("A signature file is required for this operation.", 400)

    data = await _read(file)
    sig = await _read(signature) if signature is not None else None
    job = store.create(operation, "single")
    return await _submit(background, job, _single_work(data, operation, opts, sig, file.filename), wait)


@app.post("/conversions/batch")
async def create_batch(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    operation: str = Form(...),
    options: str = Form("{}"),
    signature: Optional[UploadFile] = File(None),
    wait: bool = Form(False),
):
    opts = _parse_options(options)
    spec = ops.require_operation(operation)
    ops.validate_options(operation, opts)
    if spec.needs_signature and signature is None:
        raise pp.ImageError("A signature file is required for this operation.", 400)
    if len(files) > pp.MAX_BATCH_FILES:
        raise pp.ImageError(f"A batch can hold at most {pp.MAX_BATCH_FILES} files.", 400)

    items, total = [], 0
    for f in files:
        data = await _read(f)
        total += len(data)
        if total > pp.MAX_BATCH_BYTES:
            raise pp.ImageError("The batch is too large.", 413)
        items.append((f.filename or "image", data))
    sig = await _read(signature) if signature is not None else None

    job = store.create(operation, "batch")
    return await _submit(background, job, _batch_work(items, operation, opts, sig), wait)


@app.get("/conversions/{job_id}")
def get_conversion(job_id: str):
    job = store.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found or expired."})
    return JSONResponse(status_code=200, content=job.public())


@app.get("/conversions/{job_id}/result")
def get_result(job_id: str):
    job = store.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Result not found or expired."})
    if job.status != "succeeded" or job.path is None:
        return JSONResponse(status_code=409, content={"error": f"Result is not available (status: {job.status})."})
    return FileResponse(job.path, media_type=job.mime, filename=job.filename)


@app.delete("/conversions/{job_id}")
def delete_conversion(job_id: str):
    if not store.delete(job_id):
        return JSONResponse(status_code=404, content={"error": "Job not found or expired."})
    return {"deleted": True}


# --------------------------------------------------------------------------- #
# Convenience endpoints (same operations, image returned immediately)
# --------------------------------------------------------------------------- #
@app.post("/photos/convert")
async def convert(file: UploadFile = File(...), target_format: str = Form(...),
                  quality: Optional[int] = Form(None), flatten_background: Optional[str] = Form(None)):
    return await _direct(file, "convert", _drop_none(
        output_format=target_format, quality=quality, flatten_background=flatten_background))


@app.post("/photos/resize")
async def resize(
    file: UploadFile = File(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    scale: Optional[float] = Form(None),
    mode: Optional[str] = Form(None),
    out: dict = Depends(output_opts),
):
    return await _direct(file, "resize", {**_drop_none(width=width, height=height, scale=scale, mode=mode), **out})


@app.post("/photos/compress")
async def compress(
    file: UploadFile = File(...),
    quality: Optional[int] = Form(None),
    max_kb: Optional[int] = Form(None),
    max_dimension: Optional[int] = Form(None),
    output_format: Optional[str] = Form(None),
    flatten_background: Optional[str] = Form(None),
):
    return await _direct(file, "compress", _drop_none(
        quality=quality, max_kb=max_kb, max_dimension=max_dimension,
        output_format=output_format, flatten_background=flatten_background))


@app.post("/photos/text")
async def text(file: UploadFile = File(...), text: str = Form(...),
               style: dict = Depends(style_opts), out: dict = Depends(output_opts)):
    return await _direct(file, "text", {"text": text, **style, **out})


@app.post("/photos/date")
async def date(
    file: UploadFile = File(...),
    date: Optional[str] = Form(None, description="ISO date; omit for today"),
    use_exif: Optional[bool] = Form(None, description="use the photo's capture date when available"),
    date_format: Optional[str] = Form(None),
    style: dict = Depends(style_opts),
    out: dict = Depends(output_opts),
):
    return await _direct(file, "date", {
        **_drop_none(date=date, use_exif=use_exif, date_format=date_format), **style, **out})


@app.post("/photos/name")
async def name(file: UploadFile = File(...), name: str = Form(...),
               style: dict = Depends(style_opts), out: dict = Depends(output_opts)):
    return await _direct(file, "name", {"name": name, **style, **out})


@app.post("/photos/signature")
async def signature(
    file: UploadFile = File(...),
    signature: UploadFile = File(...),
    position: Optional[str] = Form(None),
    width_ratio: Optional[float] = Form(None),
    opacity: Optional[float] = Form(None),
    remove_background: Optional[bool] = Form(None),
    color: Optional[str] = Form(None),
    out: dict = Depends(output_opts),
):
    return await _direct(file, "signature", {**_drop_none(
        position=position, width_ratio=width_ratio, opacity=opacity,
        remove_background=remove_background, color=color), **out}, signature)