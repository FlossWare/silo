#!/usr/bin/env python3
"""Build/merge the master URL index for the scraped corpus.

Usage: build-url-master.py <master.jsonl> <scan_root> [seed.jsonl ...]

- Loads seed manifests (provenance-tagged) into a URL-keyed dict.
- Walks scan_root, extracting url/link/_source fields from JSON records.
- Writes merged master: one record per unique URL with provenance list.
Run with nice; safe to re-run (idempotent merge by URL).
"""
import json
import os
import sys
from multiprocessing import Pool

MAX_FILE_BYTES = 10 * 1024 * 1024  # skip giant files for url extraction


def extract_from_file(path):
    """Return list of (url, title, category) tuples found in one file."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(MAX_FILE_BYTES)
        rec = json.loads(head)
    except Exception:
        return []
    out = []
    records = rec if isinstance(rec, list) else [rec]
    for r in records:
        if not isinstance(r, dict):
            continue
        url = None
        for k in ("url", "link", "_source"):
            v = r.get(k)
            if isinstance(v, str) and v.startswith("http"):
                url = v
                break
        if url:
            out.append((url, r.get("title"), r.get("category")))
    return out


def main():
    master_path, scan_root = sys.argv[1], sys.argv[2]
    seeds = sys.argv[3:]

    master = {}  # url -> record
    for seed in seeds:
        for line in open(seed, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            u = r.get("url")
            if not u:
                continue
            rec = master.setdefault(u, {"url": u})
            for k in ("category", "title", "content_hash", "fetched_at",
                      "http_status", "last_checked_at", "doc_id"):
                if r.get(k) is not None and k not in rec:
                    rec[k] = r.get(k)
            prov = r.get("provenance") or ["claude-learning-db"]
            rec.setdefault("provenance", [])
            for p in prov:
                if p not in rec["provenance"]:
                    rec["provenance"].append(p)

    # Collect JSON files to scan
    paths = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames.sort()
        for fn in filenames:
            if fn.endswith(".json") or fn.endswith(".jsonl"):
                paths.append(os.path.join(dirpath, fn))
    sys.stderr.write(f"scanning {len(paths)} json files under {scan_root}\n")

    scanned = 0
    with Pool(4) as pool:
        for i, found in enumerate(pool.imap_unordered(extract_from_file, paths, chunksize=256)):
            scanned += 1
            if scanned % 200000 == 0:
                sys.stderr.write(f"  {scanned}/{len(paths)} files, {len(master)} urls\n")
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
    os.replace(tmp, master_path)
    print(f"master: {len(master)} unique urls -> {master_path} (scanned {scanned} files)")


if __name__ == "__main__":
    main()
