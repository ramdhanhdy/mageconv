"""Typer-based CLI for mageconv."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

from engine import (
    SUPPORTED_OUTPUT_FORMATS,
    convert_directory,
    convert_file,
)

app = typer.Typer(
    add_completion=False,
    help="mageconv - local-first image converter (HEIC/PNG/JPEG -> WebP/JPEG).",
)


def _fmt_size(n: Optional[int]) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


@app.command()
def convert(
    input_path: Path = typer.Argument(..., exists=True, readable=True,
                                      help="File or directory to convert."),
    output_dir: Path = typer.Option(Path("./out"), "--output-dir", "-o",
                                    help="Directory to write converted files."),
    format: str = typer.Option("webp", "--format", "-f",
                               help=f"Output format: {sorted(SUPPORTED_OUTPUT_FORMATS)}"),
    quality: int = typer.Option(85, "--quality", "-q", min=1, max=100,
                                help="Output quality (1-100)."),
    max_width: Optional[int] = typer.Option(None, "--max-width", "-w", min=1,
                                            help="Max width in px; scales down proportionally."),
    recursive: bool = typer.Option(False, "--recursive", "-r",
                                   help="Recurse into subdirectories."),
    concurrency: int = typer.Option(4, "--concurrency", "-c", min=1,
                                    help="Parallel workers for batch mode."),
):
    """Convert an image file or all supported images in a directory."""
    fmt = format.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        typer.secho(f"Unsupported format: {format}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_file():
        res = convert_file(input_path, output_dir, fmt, quality, max_width)
        if res.ok:
            typer.secho(
                f"OK  {input_path.name} -> {res.output.name} "
                f"({_fmt_size(res.original_size)} -> {_fmt_size(res.new_size)})",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(f"FAIL {input_path.name}: {res.error}",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        return

    # Directory mode
    def progress(done: int, total: int, r):
        status = "OK  " if r.ok else "FAIL"
        color = typer.colors.GREEN if r.ok else typer.colors.RED
        name = r.source.name if r.source else "?"
        extra = (f" -> {r.output.name}" if r.ok and r.output else
                 f": {r.error}")
        typer.secho(f"[{done}/{total}] {status} {name}{extra}", fg=color)

    results = asyncio.run(
        convert_directory(
            input_path, output_dir, fmt, quality, max_width,
            recursive=recursive, concurrency=concurrency, progress_cb=progress,
        )
    )

    if not results:
        typer.secho("No supported images found.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    typer.secho(f"\nDone. {ok} succeeded, {fail} failed. Output: {output_dir}",
                fg=typer.colors.CYAN)
    if fail:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
