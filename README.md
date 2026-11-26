# silo

Corpus-store tooling for a harvested knowledge base — the answer to
"do we still have everything, and what exactly is covered?"

- **`corpus_manifest.py`** — read-only integrity baseline for a corpus
  directory tree: per-directory file/byte counts and deterministic
  aggregate hashes (full sha256 for small dirs, identity hashes for
  large ones), with zero-byte/socket/symlink/stat-failure anomalies
  noted, loose root files hashed, and symlinked top-level dirs followed
  rather than silently skipped. Output is written atomically (tmp +
  rename) so a re-run can never truncate an existing baseline.
  `SILO_ROOT` env selects the store (default `~/corpus`).
- **`build-url-master.py`** — rebuilds/merges the URL master index
  (JSONL, one row per URI with provenance) by sweeping every scraped
  item in the corpus. JSONL is parsed line-by-line so large files
  contribute all their URLs (no whole-file size cap on the common
  case); an existing master is always merged first, never replaced;
  the master and seed files are excluded from the scan so
  re-ingesting them never tags false provenance. The dedup key that
  answers "have we scraped this already?" before launching anything
  new.
- **`pdf-manifest.py`** — per-PDF metadata manifest for change
  detection: one JSONL line per PDF (path, size, sha256, mtime, and a
  `%PDF` magic check that catches ciphertext masquerading as PDFs and
  truncated downloads). Hashing streams in chunks under `nice` with a
  worker pool; unreadable files produce `{path, error}` records
  instead of aborting the run. Output is atomic. `SILO_PDF_ROOTS`
  (colon-separated) and `SILO_PDF_MANIFEST` env config.
- **`silo-query.py`** — read-only query service over the artifacts the
  batch tools produce (contract: `docs/contracts/silo-query.md`).
  REST on `127.0.0.1` (`SILO_QUERY_PORT`, default 8791) plus a unix
  socket (`$XDG_RUNTIME_DIR/flossware/silo.sock`). URL-existence and
  provenance lookups against the master index (non-http URI schemes
  like `concept://` included), manifest baseline listing and
  directory-granularity diffs, PDF snapshot summaries and bounded
  per-PDF diffs. Honest-unavailable semantics: missing artifacts are
  reported per-artifact in `/ready` and return `503`, never silent
  empties; reloads keep serving last-good rows. stdlib only.

## Running the query service

```sh
SILO_ROOT=/path/to/store python3 silo-query.py
curl 'http://127.0.0.1:8791/v1/urls?url=https://example.com/'
curl --unix-socket "$XDG_RUNTIME_DIR/flossware/silo.sock" \
     http://localhost/v1/pdfs
```

Deployment convention: the batch tools run via timers and write
artifacts (including dated PDF snapshots under `pdf-snapshots/`); the
service only reads them and hot-reloads on mtime change. Unit naming
per [ADR-0034](https://github.com/FlossWare/standards/blob/main/adr/ADR-0034-runtime-function-namespaces-and-service-exposure.md):
`flossware-silo-query.service`.

## Corpus layout convention

The store these tools operate on follows one rule: **the account that
owns the data decides where it lands** — separate accounts never mix.

```
<store>/
├── public/             # shareable, no account ownership
└── private/
    └── <vertical>/     # one directory per owning account
```

Credentials never sit inside corpus directories; account config lives
outside the store and is consumed by the harvest side — see
[FlossWare/harvest](https://github.com/FlossWare/harvest) for the
scrapers that fill these dirs.

MIT licensed. Python 3.11+ stdlib only (`pdf-manifest.py` uses
`hashlib.file_digest`; the others run on older 3.x).
