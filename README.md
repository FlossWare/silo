# silo

Corpus-store tooling for a harvested knowledge base — the answer to
"do we still have everything, and what exactly is covered?"

- **`corpus_manifest.py`** — read-only integrity baseline for a corpus
  directory tree: per-directory file/byte counts and deterministic
  aggregate hashes (full sha256 for small dirs, identity hashes for
  large ones), with zero-byte/socket/symlink anomalies noted and
  symlinked top-level dirs followed rather than silently skipped.
  Re-runnable to re-baseline when the corpus changes materially.
- **`build-url-master.py`** — rebuilds the URL master index (JSONL,
  one row per URI with provenance) by sweeping every scraped item in
  the corpus. The dedup key that answers "have we scraped this
  already?" before launching anything new.

MIT licensed. Python 3 stdlib only.
