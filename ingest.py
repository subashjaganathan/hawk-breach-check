#!/usr/bin/env python3
"""Load JSONL records into Solr. Stdlib only (urllib + json) — runs on the host,
no pip install required.

Reads one JSON object per line, batches them, and POSTs arrays to
{solr}/{core}/update?commit=true. Every doc is given a unique `id`
(from --id-field if provided, otherwise generated). Malformed lines are
skipped and reported rather than crashing the run.

Breach-check mode (offline single-analyst use only): pass --email-field /
--username-field / --phone-field to hash those identifiers in place. The raw
value is normalized, replaced by its SHA-256 into a canonical field
(email_h / username_h / phone_h), and the original plaintext identifier is
dropped (unless --keep-plaintext). The /api/check endpoint hashes a typed-in
identifier the same way and exact-matches it. Normalization here MUST stay
identical to app/main.py.

Usage:
    python3 ingest.py sample.jsonl
    python3 ingest.py data.jsonl --core BigData --batch 500 --id-field event_id
    python3 ingest.py breach.jsonl --email-field email --username-field user --phone-field phone
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
import uuid


# --- identifier normalization + hashing (keep in lockstep with app/main.py) ---
def normalize(value, kind):
    v = ("" if value is None else str(value)).strip()
    if not v:
        return ""
    if kind == "phone":
        return "".join(ch for ch in v if ch.isdigit())
    return v.lower()  # email + username


def sha256_hex(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_identifier(obj, src_field, dest_field, kind, keep_plaintext):
    """Replace obj[src_field] with its SHA-256 in obj[dest_field]. Handles a
    scalar or a list of identifiers. Returns True if anything was hashed."""
    if not src_field or obj.get(src_field) in (None, ""):
        return False
    raw = obj[src_field]
    values = raw if isinstance(raw, list) else [raw]
    hashes = [sha256_hex(n) for n in (normalize(v, kind) for v in values) if n]
    if not hashes:
        return False
    obj[dest_field] = hashes if len(hashes) > 1 else hashes[0]
    if not keep_plaintext and src_field != dest_field:
        del obj[src_field]
    return True


def post_batch(solr, core, batch):
    url = f"{solr.rstrip('/')}/{core}/update?commit=true"
    payload = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
        return True, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        return False, f"HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return False, f"connection failed: {e.reason}"


def main():
    ap = argparse.ArgumentParser(description="Load JSONL records into Solr.")
    ap.add_argument("file", help="Path to JSONL file (one JSON object per line), or - for stdin")
    ap.add_argument("--solr", default="http://localhost:8983/solr",
                    help="Solr base URL (default: http://localhost:8983/solr)")
    ap.add_argument("--core", default="BigData", help="Core name (default: BigData)")
    ap.add_argument("--batch", type=int, default=1000,
                    help="Records per commit (default: 1000)")
    ap.add_argument("--id-field", default=None,
                    help="Field to use as the unique id (default: generate one)")
    ap.add_argument("--email-field", default=None,
                    help="Field holding an email; hashed into email_h (breach-check)")
    ap.add_argument("--username-field", default=None,
                    help="Field holding a username; hashed into username_h")
    ap.add_argument("--phone-field", default=None,
                    help="Field holding a phone number; hashed into phone_h")
    ap.add_argument("--keep-plaintext", action="store_true",
                    help="Keep the original identifier fields alongside the hashes "
                         "(default: drop them so the index holds no plaintext identifiers)")
    args = ap.parse_args()

    hash_spec = [
        (args.email_field, "email_h", "email"),
        (args.username_field, "username_h", "username"),
        (args.phone_field, "phone_h", "phone"),
    ]
    hashing_on = any(src for src, _, _ in hash_spec)

    batch = []
    read = sent = skipped = batch_errors = 0

    def flush():
        nonlocal sent, batch_errors
        if not batch:
            return
        ok, err = post_batch(args.solr, args.core, batch)
        if ok:
            sent += len(batch)
            print(f"  committed {len(batch)} (total sent {sent})")
        else:
            batch_errors += 1
            print(f"  batch failed: {err}", file=sys.stderr)
        batch.clear()

    if args.file == "-":
        # Read JSONL piped from convert.py (or any producer) on stdin.
        # utf-8-sig drops a leading BOM — PowerShell's native pipe prepends one,
        # which would otherwise corrupt the first record.
        try:
            sys.stdin.reconfigure(encoding="utf-8-sig")
        except Exception:
            pass
        fh = sys.stdin
    else:
        try:
            # utf-8-sig transparently strips a leading BOM if present — common on
            # JSONL written by Windows editors / PowerShell, otherwise the first
            # record would be mis-flagged as malformed.
            fh = open(args.file, "r", encoding="utf-8-sig")
        except OSError as e:
            sys.exit(f"cannot open {args.file}: {e}")

    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                skipped += 1
                print(f"  line {lineno}: malformed JSON ({e}); skipped", file=sys.stderr)
                continue
            if not isinstance(obj, dict):
                skipped += 1
                print(f"  line {lineno}: not a JSON object; skipped", file=sys.stderr)
                continue

            if hashing_on:
                for src_field, dest_field, kind in hash_spec:
                    hash_identifier(obj, src_field, dest_field, kind, args.keep_plaintext)

            if args.id_field and obj.get(args.id_field) not in (None, ""):
                obj["id"] = str(obj[args.id_field])
            elif "id" not in obj or obj.get("id") in (None, ""):
                obj["id"] = str(uuid.uuid4())

            batch.append(obj)
            read += 1
            if len(batch) >= args.batch:
                flush()
    flush()

    print(f"done: {read} read, {sent} sent, {skipped} skipped, "
          f"{batch_errors} batch error(s)")
    if batch_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
