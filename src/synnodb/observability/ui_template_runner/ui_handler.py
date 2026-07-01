"""UI handler for the BespokeOLAP HTTP service.

Serves static files from this directory:
  GET /ui        -> ui.html
  GET /style.css -> style.css
  GET /ui.js     -> ui.js
"""

from pathlib import Path

_GUI_DIR = Path(__file__).parent

_STATIC_FILES = {
    "/ui": ("ui.html", "text/html; charset=utf-8"),
    "/privacy": ("privacy.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
}

assert all((_GUI_DIR / fname).exists() for fname, _ in _STATIC_FILES.values()), (
    "One or more UI static files are missing"
)


def handle_static(path: str, handler) -> bool:
    """Serve a static file if path matches. Returns True if handled."""
    entry = _STATIC_FILES.get(path)
    if entry is None:
        return False
    fname, mime = entry
    handler._send(
        200,
        (_GUI_DIR / fname).read_bytes(),
        mime,
        extra_headers={"Cache-Control": "no-store, must-revalidate"},
    )
    return True


def handle_ui(handler) -> None:
    """Serve the UI HTML page. `handler` is a BaseHTTPRequestHandler instance."""
    handle_static("/ui", handler)
