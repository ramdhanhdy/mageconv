"""FastAPI web UI for mageconv."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from engine import (
    SUPPORTED_INPUT_EXTS,
    SUPPORTED_OUTPUT_FORMATS,
    convert_many_bytes,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="mageconv", description="Local-first image converter")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "formats": sorted(SUPPORTED_OUTPUT_FORMATS),
            "accepted": sorted(SUPPORTED_INPUT_EXTS),
        },
    )


def _fmt_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} {unit}"
        x /= 1024
    return f"{x:.1f} TB"


@app.post("/convert", response_class=HTMLResponse)
async def convert_endpoint(
    request: Request,
    files: List[UploadFile] = File(...),
    format: str = Form("webp"),
    quality: int = Form(85),
    max_width: Optional[str] = Form(None),
):
    fmt = format.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        return HTMLResponse(
            f"<div class='text-red-600'>Unsupported format: {format}</div>",
            status_code=400,
        )
    quality = max(1, min(100, int(quality)))
    mw: Optional[int] = None
    if max_width and str(max_width).strip():
        try:
            mw = int(max_width)
            if mw <= 0:
                mw = None
        except ValueError:
            mw = None

    payloads: list[tuple[str, bytes]] = []
    skipped: list[str] = []
    for uf in files:
        ext = Path(uf.filename or "").suffix.lower()
        if ext not in SUPPORTED_INPUT_EXTS:
            skipped.append(uf.filename or "?")
            continue
        data = await uf.read()
        if not data:
            skipped.append(uf.filename or "?")
            continue
        payloads.append((uf.filename or f"image{ext}", data))

    if not payloads:
        return templates.TemplateResponse(
            request,
            "_result.html",
            {
                "results": [],
                "skipped": skipped,
                "error": "No supported images uploaded.",
                "token": None,
            },
            status_code=400,
        )

    results = await convert_many_bytes(payloads, fmt, quality, mw, concurrency=4)

    # Build ZIP of successful outputs in memory.
    zip_buf = io.BytesIO()
    success = []
    failures = []
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for original, (new_name, out_bytes, err) in zip(payloads, results):
            if err or out_bytes is None:
                failures.append({"name": original[0], "error": err or "unknown"})
                continue
            zf.writestr(new_name, out_bytes)
            success.append({
                "original": original[0],
                "name": new_name,
                "original_size": _fmt_size(len(original[1])),
                "new_size": _fmt_size(len(out_bytes)),
            })

    token = _stash(zip_buf.getvalue()) if success else None

    return templates.TemplateResponse(
        request,
        "_result.html",
        {
            "results": success,
            "failures": failures,
            "skipped": skipped,
            "error": None,
            "token": token,
            "count": len(success),
        },
    )


# Very small in-memory cache for last few zipped batches (per process).
_ZIP_CACHE: dict[str, bytes] = {}
_ZIP_ORDER: list[str] = []
_ZIP_MAX = 8


def _stash(data: bytes) -> str:
    import secrets

    token = secrets.token_urlsafe(12)
    _ZIP_CACHE[token] = data
    _ZIP_ORDER.append(token)
    while len(_ZIP_ORDER) > _ZIP_MAX:
        old = _ZIP_ORDER.pop(0)
        _ZIP_CACHE.pop(old, None)
    return token


@app.get("/download/{token}")
async def download(token: str):
    data = _ZIP_CACHE.get(token)
    if data is None:
        return Response("Not found or expired.", status_code=404)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="mageconv.zip"'},
    )


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    import uvicorn

    uvicorn.run("app:app", host=host, port=port, reload=reload)
