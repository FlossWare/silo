#!/usr/bin/env python3
"""Per-PDF metadata manifest for the whole corpus (change detection).

Walks every *.pdf under corpus/public (symlinked USB store) and
corpus/private, and writes one JSONL line per file:
  {path, size, sha256, mtime, magic_ok}
magic_ok = file starts with %PDF (catches CSE ciphertext masquerading
as PDFs and truncated downloads). Unreadable files produce
{path, error} records instead of aborting the run.

Output: ~/.FlossWare/corpus/pdf-manifest.jsonl (+ .tmp then atomic move).
Re-run any time; the ingestion ledger diffs successive manifests to find
new/changed/removed PDFs. Hashing runs under nice with a worker pool,
streaming in chunks (no whole-file reads).

Requires Python >= 3.11 (hashlib.file_digest).
"""
import hashlib, json, os, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOTS = [Path(p) for p in
         (os.environ.get("SILO_PDF_ROOTS")
          or f"{Path.home() / 'corpus/public'}:{Path.home() / 'corpus/private'}").split(":")
         if p.strip()]
OUT = Path(os.environ.get("SILO_PDF_MANIFEST")
           or Path.home() / ".FlossWare/corpus/pdf-manifest.jsonl")
WORKERS = 8


def _init_worker():
    # nice ONCE per worker process — os.nice(10) is additive, so calling
    # it per file would creep to the cap (19)
    try:
        os.nice(10)
    except OSError:
        pass


def hash_one(p):
    p = Path(p)
    # MAJOR-fix: size/mtime were taken BEFORE hashing, so a file growing
    # during the digest got a hash inconsistent with its recorded size.
    # Re-fstat after hashing; retry once if the file moved under us.
    for attempt in range(2):
        try:
            with open(p, "rb") as f:
                st = os.fstat(f.fileno())
                magic = f.read(4)
                # file_digest hashes from the CURRENT offset — without the
                # seek(0) it silently hashes file[4:] and never matches sha256sum
                f.seek(0)
                h = hashlib.file_digest(f, "sha256").hexdigest()
                st2 = os.fstat(f.fileno())
                stable = (st2.st_size, st2.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
                if not stable and attempt == 0:
                    continue  # file changed during hashing — one retry
                return {"path": str(p), "size": st2.st_size, "sha256": h,
                        "mtime": st2.st_mtime, "magic_ok": magic == b"%PDF",
                        "hash_stable": stable}
        except OSError as e:
            return {"path": str(p), "error": repr(e)}
    return {"path": str(p), "error": "unstable file (changed during hashing twice)"}


def _walk_err(exc):
    print(f"  walk error: {exc}", file=sys.stderr, flush=True)


def collect_pdfs():
    # os.walk(followlinks=True), NOT rglob — the store contains symlinked
    # subdirectories (e.g. fleet-orchestrator/orchestrator-raw) that
    # Path.rglob silently refuses to descend into (missed 3,151 of
    # 10,427 PDFs that find -L sees). followlinks can loop on symlink
    # cycles, so directories are pruned by (dev, ino).
    pdfs = []
    seen_dirs = set()
    missing = [r for r in ROOTS if not r.exists()]
    if missing:
        # A missing root (e.g. unmounted drive) must NOT be treated as an
        # empty root: replacing the manifest would make the ledger see
        # every PDF under it as removed.
        for r in missing:
            print(f"root missing: {r}", file=sys.stderr, flush=True)
        sys.exit(f"aborting, manifest NOT replaced: {len(missing)} root(s) missing")
    for root in ROOTS:
        try:
            st = os.stat(root)
            seen_dirs.add((st.st_dev, st.st_ino))
        except OSError as exc:
            _walk_err(exc)
            continue
        for dirpath, dirs, names in os.walk(root, followlinks=True,
                                            onerror=_walk_err):
            pruned = []
            for d in dirs:
                try:
                    dst = os.stat(os.path.join(dirpath, d))
                except OSError as exc:
                    _walk_err(exc)
                    continue
                key = (dst.st_dev, dst.st_ino)
                if key not in seen_dirs:
                    seen_dirs.add(key)
                    pruned.append(d)
            dirs[:] = pruned
            for n in names:
                if n.lower().endswith(".pdf"):
                    p = Path(dirpath) / n
                    # is_symlink() too: a DANGLING .pdf symlink fails
                    # is_file() and would otherwise vanish with no record
                    if p.is_file() or p.is_symlink():
                        pdfs.append(str(p))
    return sorted(set(pdfs))


def main():
    pdfs = collect_pdfs()
    print(f"{len(pdfs)} PDFs to manifest", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_name(OUT.name + ".tmp")
    n = bad = errs = 0
    with tmp.open("w") as f, ProcessPoolExecutor(WORKERS,
                                                 initializer=_init_worker) as pool:
        for rec in pool.map(hash_one, pdfs, chunksize=32):
            f.write(json.dumps(rec) + "\n")
            n += 1
            if "error" in rec:
                errs += 1
            else:
                bad += not rec["magic_ok"]
            if n % 1000 == 0:
                print(f"  {n}/{len(pdfs)}", flush=True)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(OUT)
    print(f"PDF MANIFEST DONE: {n} files, {bad} failed magic check, "
          f"{errs} unreadable", flush=True)


if __name__ == "__main__":
    main()
