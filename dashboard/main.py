"""trace-dashboard: a read-only page over the trace_results views. GET only; nothing here injects, runs or writes.

The service is private (Cloud Run IAM decides who may call it), so there is no login code here."""

from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard.cache import TTLCache
from dashboard.config import Settings
from dashboard.store import ResultsStore

STATIC = Path(__file__).parent / "static"
CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
DEFAULT_KEY = "\x00latest"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _log(severity: str, message: str, **fields: object) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}), file=sys.stdout, flush=True)


def create_app(settings: Settings | None = None, store: ResultsStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or ResultsStore(settings)
    cache = TTLCache(settings.cache_ttl_s)
    app = FastAPI(title="trace-dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def load(campaign: str | None) -> dict:
        started = time.monotonic()
        snapshot = store.snapshot(campaign)
        snapshot["fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _log(
            "INFO",
            "results query",
            campaign=snapshot["campaign_id"],
            bytes_billed=snapshot["bytes_billed"],
            incidents=len(snapshot["incidents"]),
            seconds=round(time.monotonic() - started, 2),
        )
        return snapshot

    def snapshot_for(campaign: str | None) -> tuple[dict, bool]:
        """The cached snapshot. A named campaign must be one the latest snapshot lists, so a made-up name can never
        start a query."""
        try:
            latest, stale = cache.get(DEFAULT_KEY, lambda: load(None))
            if campaign is None or campaign == latest["campaign_id"]:
                return latest, stale
            if not CAMPAIGN_ID.match(campaign) or campaign not in {c["campaign_id"] for c in latest["campaigns"]}:
                raise HTTPException(status_code=404, detail="unknown campaign")
            return cache.get(campaign, lambda: load(campaign))
        except HTTPException:
            raise
        except Exception as exc:  # report it; never fall back to invented data
            _log("ERROR", "results query failed", error_class=type(exc).__name__, error=str(exc)[:500])
            raise HTTPException(status_code=502, detail=f"results query failed: {type(exc).__name__}") from exc

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/api/snapshot")
    def api_snapshot(campaign: str | None = Query(default=None, max_length=64)) -> JSONResponse:
        snapshot, stale = snapshot_for(campaign or None)
        body = {k: v for k, v in snapshot.items() if not k.startswith("_")}
        body["stale"] = stale
        body["cache_ttl_s"] = settings.cache_ttl_s
        body["dataset"] = settings.results_dataset
        return JSONResponse(body)

    @app.get("/postmortem", include_in_schema=False)
    def postmortem(campaign: str = Query(max_length=64), position: int = Query(ge=1, le=10_000)) -> HTMLResponse:
        snapshot, _ = snapshot_for(campaign)
        incident = next((i for i in snapshot["incidents"] if i.get("position") == position), None)
        if incident is None or not incident.get("postmortem_text"):
            raise HTTPException(status_code=404, detail="no postmortem for this incident")
        return HTMLResponse(render_postmortem_page(snapshot["campaign_id"], incident))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    """Escape first, then turn **bold** and `code` into tags. Nothing from the text reaches the page unescaped."""
    out = html.escape(text, quote=True)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return _CODE.sub(r"<code>\1</code>", out)


def render_markdown(text: str) -> str:
    """A deliberately small Markdown subset (headings, bullet and numbered lists, paragraphs, fenced code)."""
    parts: list[str] = []
    list_tag: str | None = None
    in_code = False
    paragraph: list[str] = []

    def close_paragraph() -> None:
        if paragraph:
            parts.append("<p>" + " ".join(paragraph) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            close_paragraph()
            close_list()
            parts.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            parts.append(html.escape(raw) + "\n")
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if not line.strip():
            close_paragraph()
            close_list()
        elif heading:
            close_paragraph()
            close_list()
            level = min(len(heading.group(1)) + 1, 6)  # the page already has an h1
            parts.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            close_paragraph()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                parts.append(f"<{tag}>")
                list_tag = tag
            parts.append(f"<li>{_inline((bullet or numbered).group(1))}</li>")
        else:
            close_list()
            paragraph.append(_inline(line.strip()))
    close_paragraph()
    close_list()
    if in_code:
        parts.append("</code></pre>")
    return "\n".join(parts)


def render_postmortem_page(campaign_id: str, incident: dict) -> str:
    esc = html.escape
    title = f"Postmortem · {campaign_id} #{incident['position']}"
    reason = incident.get("postmortem_fallback_reason")
    fallback = f" · fallback after: {esc(str(reason)[:120])}" if reason else ""
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="static/app.css">
<script src="static/theme.js"></script>
</head>
<body class="doc">
<main class="doc-page">
<p class="muted"><a href="./#incidents">&larr; Back to the dashboard</a></p>
<h1>{esc(title)}</h1>
<p class="muted">Execution <code>{esc(str(incident.get("failed_execution") or "n/a"))}</code> ·
written by <code>{esc(str(incident.get("postmortem_model") or "n/a"))}</code>{fallback}</p>
<article class="markdown">
{render_markdown(incident["postmortem_text"])}
</article>
</main>
</body>
</html>"""


def __getattr__(name: str):  # `uvicorn dashboard.main:app` builds the app on first use; importing for tests does not
    if name == "app":
        return create_app()
    raise AttributeError(name)
