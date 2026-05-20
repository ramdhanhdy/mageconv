"""FastAPI web UI for mageconv."""
from __future__ import annotations

import io
import os
import sys
import subprocess
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
    output_dir: str = Form("./out"),
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
                "output_dir": None,
            },
            status_code=400,
        )

    results = await convert_many_bytes(payloads, fmt, quality, mw, concurrency=4)

    # Save to output_dir directly
    out_dir = Path(output_dir.strip() if output_dir.strip() else "./out")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dir_error = None
    except Exception as e:
        dir_error = f"Could not create output directory: {e}"

    success = []
    failures = []

    if not dir_error:
        for original, (new_name, out_bytes, err) in zip(payloads, results):
            if err or out_bytes is None:
                failures.append({"name": original[0], "error": err or "unknown"})
                continue
            try:
                out_path = out_dir / new_name
                out_path.write_bytes(out_bytes)
                success.append({
                    "original": original[0],
                    "name": new_name,
                    "original_size": _fmt_size(len(original[1])),
                    "new_size": _fmt_size(len(out_bytes)),
                })
            except Exception as ex:
                failures.append({"name": original[0], "error": f"Failed to write to disk: {ex}"})
    else:
        for original, _ in payloads:
            failures.append({"name": original, "error": dir_error})

    resolved_path = str(out_dir.resolve()) if not dir_error else None

    return templates.TemplateResponse(
        request,
        "_result.html",
        {
            "results": success,
            "failures": failures,
            "skipped": skipped,
            "error": None,
            "output_dir": resolved_path,
            "count": len(success),
        },
    )


@app.post("/open-folder")
async def open_folder(folder_path: str = Form(...)):
    path = Path(folder_path)
    if path.exists() and path.is_dir():
        if os.name == 'nt':
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(path)])
        else:
            subprocess.Popen(['xdg-open', str(path)])
        return Response(status_code=200)
    return Response("Folder not found", status_code=400)


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    import uvicorn

    uvicorn.run("app:app", host=host, port=port, reload=reload)
