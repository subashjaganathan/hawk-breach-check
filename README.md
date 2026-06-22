# Ledger — Self-Hosted Full-Text Search

A small, self-hosted full-text search engine over **your own records** — logs,
analyst notes, threat-feed documents, synthetic datasets. It is **not** a public
search service and is not designed to be one.

```
docker-compose.yml      Solr + FastAPI proxy, wired together
app/
  Dockerfile            python:3.12-slim, uvicorn on :3000
  requirements.txt      fastapi, uvicorn[standard], httpx
  main.py               API + serves the UI
  static/index.html     search UI (vanilla JS, no build step)
  static/check.html     breach-check UI (offline single-analyst use)
convert.py              turns Excel/CSV/txt/JSON into JSONL (runs on the host)
ingest.py               stdlib-only JSONL loader (+ identifier hashing), runs on the host
sample.jsonl            15 synthetic DFIR records to get started
sample_breach.jsonl     5 synthetic breach records for the breach-check demo
README.md
```

## Architecture

```
browser ──▶ FastAPI proxy (:3000) ──▶ Solr core "BigData" (:8983, localhost-only)
            /api/search                _default configset
            /api/fields                schemaless field-guessing
            /api/health                _text_ catch-all copyField
            /  (static UI)
```

The browser **never** talks to Solr directly. The proxy is the only surface the
UI uses, which is also where you'd later bolt on auth, rate-limiting, or query
logging.

> **Note on `_text_`:** Solr 9's `_default` configset defines the `_text_`
> field but ships **no** `* -> _text_` catch-all copyField, so free-text search
> (`df=_text_`) would match nothing out of the box. The proxy adds that
> copyField on startup if it's missing (`_ensure_text_catchall` in `main.py`),
> so search works on first ingest. Because a copyField only affects documents
> indexed *after* it exists, always start the stack before ingesting.

## Quickstart

```bash
docker compose up --build
```

Wait for the Solr healthcheck to pass (the **solr** dot in the UI turns green),
then open:

```
http://localhost:3000
```

On a clean start the index is empty — the UI will say *"No records match."*
Load the sample data (below) and search again.

## Ingesting data

`ingest.py` is stdlib-only (no `pip install`) and runs on the **host**, not in a
container:

```bash
python3 ingest.py sample.jsonl
```

Other options:

```bash
python3 ingest.py data.jsonl --core BigData --batch 500
python3 ingest.py events.jsonl --id-field event_id     # use an existing field as the id
python3 ingest.py data.jsonl --solr http://localhost:8983/solr
```

- **Input format:** JSONL — one JSON object per line. Each object is one
  record/document; its keys become Solr fields.
- **IDs:** every doc needs a unique `id`. If `--id-field` is given, that field's
  value is copied to `id`; otherwise, if no `id` is present, one is generated
  (UUID). Re-ingesting a doc with the same `id` overwrites it.
- **Bad lines** are skipped and reported — a malformed line never aborts the run.
- Data is committed in batches (default 1000) so it's searchable immediately.
- Reading from **stdin**: pass `-` as the file to ingest piped JSONL (see below).

### Importing Excel / CSV / text / other formats

The index only eats JSONL, so `convert.py` turns the common formats into it
first. Point it at a **file or a whole directory** (walked recursively):

| Source | Handling |
|--------|----------|
| `.csv` | Delimiter + header auto-detected; header row → field names. |
| `.tsv` / `.tab` | Tab-separated; header row → field names. |
| `.xlsx` | Each sheet's rows → records; first row = headers. Needs `pip install openpyxl`. (`.xls` not supported — re-save as `.xlsx`/CSV.) |
| `.txt` / combolists | One line per record. Delimiter auto-detected; `email:password` assumed for 2 columns, else pass `--columns`. |
| `.json` | A JSON array of objects (or a single object). |
| `.jsonl` / `.ndjson` | Passed through (and validated). |

Two conveniences are on by default: identifier columns are **canonicalized**
(`mail`, `e-mail`, `login`, `tel`, `mobile`, … → `email`/`username`/`phone`) so
one ingest command fits every file, and each record gets a **`source_file`**
field for provenance.

```bash
# whole folder of mixed files -> hashed + loaded in one pipe
python convert.py "C:/leaks" | python ingest.py - --email-field email --username-field username --phone-field phone

# headerless combolist with a custom layout
python convert.py combo.txt --delimiter : --columns email,password,ip | python ingest.py - --email-field email
```

> **Windows / PowerShell tip:** the native pipe is fine (`ingest.py` strips the
> BOM PowerShell injects), but the most robust path is to write a file first,
> then ingest it — handy for large datasets anyway:
> ```powershell
> python convert.py "C:\leaks" --out converted.jsonl
> python ingest.py converted.jsonl --email-field email --username-field username --phone-field phone
> ```

Useful `convert.py` flags: `--out FILE`, `--delimiter`, `--columns a,b,c`,
`--no-header`, `--sheet NAME` (Excel), `--no-canonical`, `--no-source-file`.

### Record / JSONL format

Records are free-form. Solr is in schemaless mode, so fields are type-guessed on
first sight; the proxy ensures a `* -> _text_` catch-all copyField exists (see
the note above), so full-text search works on the first ingest. Example lines:

```json
{"id": "note-0002", "kind": "analyst_note", "title": "Beaconing host 10.0.7.88", "body": "Regular 60s callouts to cdn-update[.]example over 443...", "severity": "high", "tags": ["c2", "beacon"]}
{"id": "feed-0001", "kind": "threat_feed", "indicator": "cdn-update.example", "indicator_type": "domain", "confidence": "high"}
```

The UI adapts to whatever fields you load (via `/api/fields`) and renders each
record as a card of `field → value` rows.

### Searching

The search box maps directly to Solr's edismax query parser with `_text_` as the
default field:

- `cobaltstrike` — any record mentioning it
- `severity:high` — field-scoped
- `c2 AND powershell` — boolean
- blank box — lists all records (`*:*`)

Matched terms are highlighted. The readout shows total hits, round-trip latency,
the current page, and a Solr health dot.

## API

| Route | Purpose |
|-------|---------|
| `GET /api/search?q=&start=&rows=&sort=` | Proxies to Solr `select` (edismax, `df=_text_`, highlighting on). Empty `q` → `*:*`. `rows` clamped to `MAX_ROWS`. Returns `{total, start, rows, query, docs, highlighting}`. Solr 4xx → clean `400` with the Solr message; unreachable Solr → `503`. |
| `GET /api/fields` | Non-underscore schema fields, so the UI adapts to your data. |
| `GET /api/check?email=&username=&phone=` | **Breach check (offline only).** Hashes each supplied identifier (SHA-256 of the normalized value) and exact-matches it against `email_h`/`username_h`/`phone_h`, returning the **full** matching records. Returns `{found, count, checked, docs}`. See the breach-check section. |
| `GET /api/health` | Pings Solr, returns `{ok, core}`. |

Configured via env (set in `docker-compose.yml`): `SOLR_URL`, `SOLR_CORE`,
`MAX_ROWS`.

## Breach check (offline, single-analyst only)

A second page — **`/check.html`** ("Breach check" in the nav) — lets you enter an
email, username, or phone and see whether it appears in indexed breach records.

> ⚠️ **This feature returns full record contents (passwords, PII) on a match.**
> That is only acceptable because this stack is bound to **localhost for a single
> analyst** doing research on their own dataset. **Do not expose `/api/check` (or
> the stack) to a network, other users, or the internet.** A reachable endpoint
> that hands back other people's breached passwords by email is an abuse tool, not
> a safety tool. If you ever need others to self-check, switch to returning
> *metadata only* (which breaches + exposed data classes, never contents) and add
> owner-verification + rate-limiting at the proxy first.

**How it works**

- Identifiers are **hashed at ingest**, not stored in plaintext. Pass the
  identifier fields to `ingest.py`:
  ```bash
  python3 ingest.py breach.jsonl \
    --email-field email --username-field username --phone-field phone
  ```
  Each value is normalized (email/username → trim + lowercase; phone → digits
  only), SHA-256'd, and written to `email_h` / `username_h` / `phone_h`. The
  original plaintext identifier field is **dropped** (use `--keep-plaintext` to
  retain it). Non-identifier fields (password, source, date, …) are kept as-is so
  the full record is still useful.
- `/api/check` applies the **same** normalization + hash to what you type and
  exact-matches the hash, so the index never needs — and never holds — the
  plaintext identifier. Matching is case-insensitive for email/username and
  format-insensitive for phone (it compares digits only, so country-code
  differences can cause misses).
- Try it with the bundled synthetic data:
  ```bash
  python3 ingest.py sample_breach.jsonl --email-field email --username-field username --phone-field phone
  # then open /check.html and check  alice@example.com  (mixed case works)
  ```

## Security

This stack is built for indexing **your own** records on a machine **you**
control. Keep it that way:

- **8983 stays localhost-only.** Solr's admin UI has **no authentication** and
  must never face the network. The compose file binds it to `127.0.0.1:8983`;
  do not change that to `0.0.0.0` or publish it through a reverse proxy without
  putting auth in front of it first. Anyone who can reach Solr directly can read,
  modify, or delete your entire index — and in some configurations run code.
- **The proxy is the only intended public surface.** `app/main.py` is the single
  seam for adding **authentication, rate-limiting, and query logging**. It also
  exposes nothing but the three `/api/*` routes plus the static UI, and disables
  Solr's raw admin/update endpoints to the browser. The proxy port is *also*
  bound to localhost by default — if you expose it (e.g. on a LAN or behind a
  tunnel), add auth at the proxy first.
- **Move off schemaless once your fields are settled.** The `_default` configset
  is convenient for getting started, but field-guessing is brittle (a single
  oddly-typed value can lock a field to the wrong type) and the index is larger
  than it needs to be. Once you know your fields, define an **explicit schema**:
  pick `string` (exact match / faceting / sorting — e.g. `id`, `severity`,
  `indicator_type`, IPs) vs. `text_general` (tokenized full-text — e.g. `body`,
  `title`, `event`), set `indexed`/`stored` deliberately, and only copy the
  fields you actually search into `_text_`. The payoff is better relevance and a
  smaller, faster index.

## Notes

- Solr data persists in the named Docker volume `solr_data`. `docker compose
  down` keeps it; `docker compose down -v` wipes the index.
- To reset the index without removing the volume:
  `python3 ingest.py` won't clear it — delete via
  `curl 'http://localhost:8983/solr/BigData/update?commit=true' -H 'Content-Type: application/json' -d '{"delete":{"query":"*:*"}}'`.
