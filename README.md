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
