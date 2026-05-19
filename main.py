"""Entry point for mageconv.

Usage:
    # CLI (default):
    python main.py convert ./photos --output-dir ./out --format webp --quality 85 --max-width 1920

    # Web UI:
    python main.py serve --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import typer

from cli import convert as _convert_cmd
from app import run as run_server

app = typer.Typer(add_completion=False, help="mageconv - local image converter.")
app.command("convert")(_convert_cmd)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Run the web UI."""
    run_server(host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
