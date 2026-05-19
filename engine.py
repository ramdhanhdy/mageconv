"""Core image conversion engine.

Handles ingestion, EXIF stripping, resizing, and format conversion for
HEIC / PNG / JPEG inputs into WebP / JPEG outputs.

All operations run locally; output files are written without EXIF metadata.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional, but listed in requirements
    pillow_heif = None  # type: ignore[assignment]


SUPPORTED_INPUT_EXTS: frozenset[str] = frozenset(
    {".heic", ".heif", ".png", ".jpg", ".jpeg"}
)
SUPPORTED_OUTPUT_FORMATS: frozenset[str] = frozenset({"webp", "jpeg"})

_FORMAT_TO_EXT = {"webp": ".webp", "jpeg": ".jpg"}
_FORMAT_TO_PIL = {"webp": "WEBP", "jpeg": "JPEG"}


@dataclass
class ConvertResult:
    """Result of converting a single image."""

    source: Optional[Path]
    output: Optional[Path]
    ok: bool
    error: Optional[str] = None
    original_size: Optional[int] = None
    new_size: Optional[int] = None


def _normalize_format(fmt: str) -> str:
    f = fmt.lower().lstrip(".")
    if f == "jpg":
        f = "jpeg"
    if f not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(
            f"Unsupported output format: {fmt!r}. "
            f"Choose one of: {sorted(SUPPORTED_OUTPUT_FORMATS)}"
        )
    return f


def _prepare_image(
    img: Image.Image,
    out_format: str,
    max_width: Optional[int],
) -> Image.Image:
    """Resize and mode-convert an image in preparation for encoding.

    Strips EXIF by constructing a fresh image (no `info`/`exif` carry-over).
    """
    # Resize proportionally if needed.
    if max_width and img.width > max_width:
        ratio = max_width / float(img.width)
        new_size = (max_width, max(1, int(round(img.height * ratio))))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    # JPEG cannot carry alpha; flatten onto white.
    if out_format == "jpeg":
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
    else:  # webp
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    # Strip metadata: copy pixel data into a brand-new Image instance.
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))
    return clean


def _encode(
    img: Image.Image,
    out_format: str,
    quality: int,
) -> bytes:
    buf = io.BytesIO()
    save_kwargs: dict = {"format": _FORMAT_TO_PIL[out_format], "quality": int(quality)}
    if out_format == "jpeg":
        save_kwargs["optimize"] = True
        save_kwargs["progressive"] = True
    elif out_format == "webp":
        save_kwargs["method"] = 6
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def convert_bytes(
    data: bytes,
    out_format: str = "webp",
    quality: int = 85,
    max_width: Optional[int] = None,
) -> bytes:
    """Convert raw image bytes to the target format, returning new bytes.

    EXIF metadata is stripped. Pure in-memory; safe for web uploads.
    """
    fmt = _normalize_format(out_format)
    if not 1 <= int(quality) <= 100:
        raise ValueError("quality must be between 1 and 100")

    with Image.open(io.BytesIO(data)) as src:
        src.load()
        prepared = _prepare_image(src, fmt, max_width)
        return _encode(prepared, fmt, quality)


def convert_file(
    input_path: Union[str, Path],
    output_dir: Union[str, Path],
    out_format: str = "webp",
    quality: int = 85,
    max_width: Optional[int] = None,
    overwrite: bool = True,
) -> ConvertResult:
    """Convert a single image file on disk."""
    in_path = Path(input_path)
    out_dir = Path(output_dir)
    fmt = _normalize_format(out_format)

    if not in_path.is_file():
        return ConvertResult(in_path, None, False, "input file not found")
    if in_path.suffix.lower() not in SUPPORTED_INPUT_EXTS:
        return ConvertResult(
            in_path, None, False, f"unsupported input extension: {in_path.suffix}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (in_path.stem + _FORMAT_TO_EXT[fmt])
    if out_path.exists() and not overwrite:
        return ConvertResult(in_path, out_path, False, "output exists")

    try:
        original_size = in_path.stat().st_size
        with Image.open(in_path) as src:
            src.load()
            prepared = _prepare_image(src, fmt, max_width)
            encoded = _encode(prepared, fmt, quality)
        out_path.write_bytes(encoded)
        return ConvertResult(
            in_path, out_path, True,
            original_size=original_size, new_size=len(encoded),
        )
    except Exception as e:  # noqa: BLE001 - report any conversion failure
        return ConvertResult(in_path, out_path, False, f"{type(e).__name__}: {e}")


def _iter_input_files(path: Path, recursive: bool) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    it = path.rglob("*") if recursive else path.glob("*")
    for p in it:
        if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_EXTS:
            yield p


async def _run_in_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def convert_directory(
    input_path: Union[str, Path],
    output_dir: Union[str, Path],
    out_format: str = "webp",
    quality: int = 85,
    max_width: Optional[int] = None,
    recursive: bool = False,
    concurrency: int = 4,
    progress_cb=None,
) -> List[ConvertResult]:
    """Async batch convert all supported images under `input_path`.

    `progress_cb(done, total, result)` is invoked after each file, if provided.
    """
    in_path = Path(input_path)
    out_dir = Path(output_dir)
    files = list(_iter_input_files(in_path, recursive))
    total = len(files)
    if total == 0:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))
    results: List[ConvertResult] = []
    done = 0
    lock = asyncio.Lock()

    async def worker(fp: Path) -> ConvertResult:
        nonlocal done
        async with sem:
            res = await _run_in_thread(
                convert_file, fp, out_dir, out_format, quality, max_width, True
            )
        async with lock:
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total, res)
                except Exception:
                    pass
        return res

    results = await asyncio.gather(*(worker(f) for f in files))
    return results


async def convert_many_bytes(
    items: Sequence[tuple[str, bytes]],
    out_format: str = "webp",
    quality: int = 85,
    max_width: Optional[int] = None,
    concurrency: int = 4,
) -> List[tuple[str, Optional[bytes], Optional[str]]]:
    """Convert (filename, bytes) pairs concurrently. Returns (new_name, data, error)."""
    fmt = _normalize_format(out_format)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(name: str, data: bytes):
        async with sem:
            try:
                out = await _run_in_thread(
                    convert_bytes, data, fmt, quality, max_width
                )
                new_name = Path(name).stem + _FORMAT_TO_EXT[fmt]
                return (new_name, out, None)
            except Exception as e:  # noqa: BLE001
                return (name, None, f"{type(e).__name__}: {e}")

    return await asyncio.gather(*(one(n, d) for n, d in items))
