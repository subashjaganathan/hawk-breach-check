"""Thin FastAPI proxy in front of Apache Solr.

The browser never talks to Solr directly. Only the routes below are exposed,
and static/ is mounted last so the API always takes precedence. This proxy is
the single public surface and the natural seam for adding auth, rate-limiting,
or query-logging later.
"""

import hashlib
import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

SOLR_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr").rstrip("/")
SOLR_CORE = os.environ.get("SOLR_CORE", "BigData")
MAX_ROWS = int(os.environ.get("MAX_ROWS", "100"))

SOLR_BASE = f"{SOLR_URL}/{SOLR_CORE}"

app = FastAPI(title="Ledger Search", docs_url=None, redoc_url=None)
client = httpx.AsyncClient(timeout=15.0)


@app.on_event("startup")
async def _startup() -> None:
    await _ensure_text_catchall()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await client.aclose()


async def _ensure_text_catchall() -> None:
    """Solr 9's _default configset defines the _text_ field but ships NO
    catch-all copyField into it, so `df=_text_` (our whole free-text design)
    matches nothing until one exists. Add `* -> _text_` if it's missing.

    Idempotent and non-fatal: a copyField only affects docs indexed *after*
    it's added, so this must run before ingest. The app still starts if Solr
    is briefly unavailable here (depends_on:service_healthy makes that rare).
    """
    try:
        resp = await client.get(f"{SOLR_BASE}/schema/copyfields", params={"wt": "json"})
        if resp.status_code == 200:
            existing = resp.json().get("copyFields", [])
            if any(c.get("source") == "*" and c.get("dest") == "_text_" for c in existing):
                return
        await client.post(
            f"{SOLR_BASE}/schema",
            json={"add-copy-field": {"source": "*", "dest": "_text_"}},
        )
    except Exception:
        pass


def _solr_error_message(resp: httpx.Response) -> str:
    """Pull the human-readable message out of a Solr error response."""
    try:
        body = resp.json()
        err = body.get("error", {})
        if isinstance(err, dict):
            return err.get("msg") or str(err)
        return str(err)
    except Exception:
        return (resp.text or "").strip()[:500] or f"Solr returned {resp.status_code}"


async def _solr_get(path: str, params: dict) -> httpx.Response:
    """GET against Solr, mapping transport failures to a 503."""
    try:
        return await client.get(f"{SOLR_BASE}{path}", params=params)
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Solr is unreachable")


@app.get("/api/search")
async def search(q: str = "", start: int = 0, rows: int = 10, sort: str = ""):
    q = q.strip() or "*:*"
    rows = max(0, min(rows, MAX_ROWS))
    start = max(0, start)

    params = {
        "q": q,
        "defType": "edismax",
        "df": "_text_",
        "start": start,
        "rows": rows,
        "hl": "true",
        "hl.fl": "*",
        "wt": "json",
    }
    sort = sort.strip()
    if sort:
        params["sort"] = sort

    resp = await _solr_get("/select", params)
    if resp.status_code >= 400:
        # Solr 4xx (e.g. bad sort field, malformed query) -> clean 400.
        raise HTTPException(status_code=400, detail=_solr_error_message(resp))

    data = resp.json()
    body = data.get("response", {})
    return {
        "total": body.get("numFound", 0),
        "start": body.get("start", start),
        "rows": rows,
        "query": q,
        "docs": body.get("docs", []),
        "highlighting": data.get("highlighting", {}),
    }


@app.get("/api/fields")
async def fields():
    """Non-underscore schema fields, so the UI adapts to whatever was loaded."""
    resp = await _solr_get("/schema/fields", {"wt": "json"})
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=_solr_error_message(resp))
    names = [
        f["name"]
        for f in resp.json().get("fields", [])
        if not f.get("name", "").startswith("_")
    ]
    return {"fields": sorted(names)}


@app.get("/api/health")
async def health():
    try:
        resp = await client.get(f"{SOLR_BASE}/admin/ping", params={"wt": "json"})
        ok = resp.status_code == 200 and resp.json().get("status") == "OK"
    except Exception:
        ok = False
    return {"ok": ok, "core": SOLR_CORE}


# --- breach check (offline single-analyst use only) -------------------------
# Normalization MUST stay identical to ingest.py, or a typed identifier won't
# hash to the same value that was indexed.
def _normalize(value: str, kind: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if kind == "phone":
        return "".join(ch for ch in v if ch.isdigit())
    return v.lower()  # email + username


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@app.get("/api/check")
async def check(email: str = "", username: str = "", phone: str = ""):
    """Hash each supplied identifier and exact-match it against the hashed
    fields, returning the full matching breach records.

    WARNING: this returns raw record contents. It is only safe because the
    stack is bound to localhost for a single analyst. Do not expose it.
    """
    clauses, checked = [], []
    for raw, kind, field in (
        (email, "email", "email_h"),
        (username, "username", "username_h"),
        (phone, "phone", "phone_h"),
    ):
        norm = _normalize(raw, kind)
        if norm:
            clauses.append(f"{field}:{_sha256(norm)}")
            checked.append(kind)

    if not clauses:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: email, username, phone",
        )

    resp = await _solr_get("/select", {"q": " OR ".join(clauses), "rows": MAX_ROWS, "wt": "json"})
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=_solr_error_message(resp))

    body = resp.json().get("response", {})
    return {
        "found": body.get("numFound", 0) > 0,
        "count": body.get("numFound", 0),
        "checked": checked,
        "docs": body.get("docs", []),
    }


# Mounted last: API routes above always win over static file serving.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
