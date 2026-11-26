#!/usr/bin/env python3
"""Build/merge the master URL index for the scraped corpus.

Usage: build-url-master.py <master.jsonl> <scan_root> [seed.jsonl ...]

- Loads the existing master (if present) and seed manifests
  (provenance-tagged) into a URL-keyed dict.
- Walks scan_root, extracting url/link/_source fields from JSON records
  and line-by-line from JSONL files.
- Writes merged master: one record per unique URL with provenance list.
Run with nice; safe to re-run (idempotent merge by URL, existing master
is always loaded first and merged, never replaced). A master line that
cannot be parsed is FATAL: rewriting the master without it would be
silent data loss.
"""
import json
import os
import sys
from multiprocessing import Pool

MAX_FILE_BYTES = 10 * 1024 * 1024  # skip giant single-record JSON files
MAX_LINE_BYTES = 10 * 1024 * 1024  # skip pathological JSONL lines (true bytes)


def _collect(rec, out):
    """Append (url, title, category) for each dict record in rec."""
    records = rec if isinstance(rec, list) else [rec]
    for r in records:
        if not isinstance(r, dict):
            continue
        for k in ("url", "link", "_source"):
            v = r.get(k)
            if isinstance(v, str) and v.startswith("http"):
                out.append((v, r.get("title"), r.get("category")))
                break


def extract_from_file(path):
    """Return ([(url, title, category)...], lines_skipped, file_skipped).

    Lines/files are read as BYTES and decoded per line, so a line with
    invalid UTF-8 is skipped and counted instead of being silently
    corrupted into a bogus URL key (which is what errors="replace" did).
    len() on the binary line is a true byte count.
    """
    file_skipped = 0
    lines_skipped = 0
    out = []
    if path.endswith(".jsonl"):
        # JSONL: one JSON record per line; parse line by line so
        # multi-record files contribute all their URLs. NO file-size
        # cap here — large JSONL is the common case and dropping a
        # whole file silently loses every URL in it.
        try:
            with open(path, "rb") as f:
                for bline in f:
                    if len(bline) > MAX_LINE_BYTES:
                        lines_skipped += 1
                        continue
                    try:
                        line = bline.decode("utf-8")
                    except UnicodeDecodeError:
                        lines_skipped += 1
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        lines_skipped += 1
                        continue
                    _collect(rec, out)
        except OSError:
            return [], lines_skipped, 1
    else:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                return [], 0, 1
        except OSError:
            return [], 0, 1
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_FILE_BYTES)
            rec = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError):
            return [], 0, 1
        except Exception:
            return [], 0, 1
        _collect(rec, out)
    return out, lines_skipped, file_skipped


def _scan(path):
    """extract_from_file with the path kept for skip-reason reporting."""
    found, ls, fs = extract_from_file(path)
    return path, found, ls, fs


def main():
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <master.jsonl> <scan_root> [seed.jsonl ...]",
              file=sys.stderr)
        sys.exit(2)
    master_path, scan_root = sys.argv[1], sys.argv[2]
    seeds = sys.argv[3:]

    if not os.path.isdir(scan_root):
        print(f"error: scan_root {scan_root!r} is not a directory", file=sys.stderr)
        sys.exit(2)

    # Single-writer lock: the read->merge->replace on the master loses
    # additions if two runs interleave. Hold an exclusive lock for the
    # whole run (batch tool, single operator by design).
    import fcntl
    lock_fh = open(master_path + ".lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"FATAL: another build-url-master run holds {master_path + '.lock'}")

    master = {}  # url -> record

    def load_seed(path, default_prov):
        """Merge one JSONL file into master. Returns dropped-line count."""
        dropped = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    dropped += 1
                    continue
                if not isinstance(r, dict):
                    dropped += 1
                    continue
                u = r.get("url")
                # master/seed rows may carry non-http URI schemes (e.g.
                # concept:// synthesized identifiers from concept-mining
                # passes) — they are valid index rows. Only require a
                # scheme separator. (The scan side stays http-only: real
                # web URLs only.)
                if not isinstance(u, str) or "://" not in u:
                    dropped += 1
                    continue
                rec = master.setdefault(u, {"url": u})
                for k in ("category", "title", "content_hash", "fetched_at",
                          "http_status", "last_checked_at", "doc_id"):
                    if r.get(k) is not None and k not in rec:
                        rec[k] = r.get(k)
                prov = r.get("provenance")
                if prov is None:
                    prov = [default_prov]
                elif isinstance(prov, str):
                    prov = [prov]
                elif not isinstance(prov, list):
                    # a dict would iterate its keys into tags; anything
                    # else (int/bool) would crash — fall back instead
                    prov = [default_prov]
                # an explicit empty list stays empty: `or` would have
                # silently rewritten the author's provenance
                rec.setdefault("provenance", [])
                for p in prov:
                    if p not in rec["provenance"]:
                        rec["provenance"].append(p)
        return dropped

    # Existing master is always merged first (never replaced). Dropping
    # lines HERE is fatal: the merged write-back would lose them.
    if os.path.exists(master_path):
        dropped = load_seed(master_path, "existing-master")
        if dropped:
            sys.exit(f"FATAL: {dropped} unparseable line(s) in existing master "
                     f"{master_path!r}; refusing to rewrite the master without them")
    for seed in seeds:
        if not os.path.isfile(seed):
            print(f"error: seed {seed!r} not found", file=sys.stderr)
            sys.exit(2)
        dropped = load_seed(seed, "seed-file")
        if dropped:
            print(f"warning: {dropped} unparseable line(s) dropped from seed "
                  f"{seed!r}", file=sys.stderr)

    # Collect JSON files to scan, excluding the master and seed files
    # themselves. Compare REALPATHS: abspath does not resolve symlinks,
    # so the master/seed reachable under scan_root through a symlinked
    # path would be re-ingested and every URL retagged with false
    # provenance.
    exclude = {os.path.realpath(master_path)} | {os.path.realpath(s) for s in seeds}
    paths = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames.sort()
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if (fn.endswith(".json") or fn.endswith(".jsonl")) \
                    and os.path.realpath(p) not in exclude:
                paths.append(p)
    sys.stderr.write(f"scanning {len(paths)} json/jsonl files under {scan_root}\n")

    scanned = 0
    skipped_files = 0
    skipped_lines = 0
    with Pool(4) as pool:
        for path, found, ls, fs in pool.imap_unordered(_scan, paths, chunksize=256):
            scanned += 1
            skipped_lines += ls
            skipped_files += fs
            if fs:
                sys.stderr.write(f"  skipped whole file: {path}\n")
            if scanned % 200000 == 0:
                sys.stderr.write(f"  {scanned}/{len(paths)} files, {len(master)} urls"
                                 f" (skipped files={skipped_files} lines={skipped_lines})\n")
            for url, title, category in found:
                rec = master.setdefault(url, {"url": url})
                if title and not rec.get("title"):
                    rec["title"] = title
                if category and not rec.get("category"):
                    rec["category"] = category
                rec.setdefault("provenance", [])
                if "web-scrape-corpus" not in rec["provenance"]:
                    rec["provenance"].append("web-scrape-corpus")

    tmp = master_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for u in sorted(master):
            f.write(json.dumps(master[u], ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())   # power-loss durability; replace alone is not
    os.replace(tmp, master_path)
    print(f"master: {len(master)} unique urls -> {master_path} "
          f"(scanned {scanned} files; skipped files={skipped_files} "
          f"lines={skipped_lines})")
    if skipped_files or skipped_lines:
        # MAJOR-fix: scan losses were stderr-only and the master was
        # replaced without them — make the loss unmissable on stdout
        print(f"WARNING: master written WITHOUT {skipped_files} skipped files "
              f"and {skipped_lines} skipped lines (details on stderr above)",
              flush=True)


if __name__ == "__main__":
    main()
