#!/usr/bin/env python3
"""FlossWare corpus manifest generator (READ-ONLY).

Produces ~/.FlossWare/corpus-manifest-<date>.json + .md — a one-time
integrity baseline of the whole store.

Method (documented in the manifest under "method"):
  - Walk every file under ~/.FlossWare (nothing excluded; the manifest
    output files themselves are skipped to avoid self-reference).
  - Top-level directories that are symlinks (e.g., sources/ moved to an
    external drive) are FOLLOWED: walked via their target and recorded
    with "symlink_target" so the manifest still covers the whole corpus.
  - Per TOP-LEVEL directory: file count, total bytes, and a deterministic
    aggregate hash = sha256 over the sorted list of lines
    "<relpath> <size> <entry-hash>", where:
      * small directories (total <= 4 GiB) use FULL per-file sha256 of
        contents as the entry hash;
      * large directories use an identity hash
        sha256("<relpath>|<size>|<mtime_ns>") — cheaper, still detects
        moves/size/mtime changes but not in-place content edits.
  - Sockets/FIFOs and zero-byte files are counted and reported, not hashed.
  - Hashing runs under `nice` with a bounded worker pool (default 4).

Re-run:  python3 ~/.FlossWare/tools/corpus_manifest.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

STORE = Path.home() / ".FlossWare"
SMALL_DIR_BYTES = 4 * 1024**3  # full per-file hashing threshold
WORKERS = 4
SKIP_NAMES = set()  # output files added at runtime (see main)

SKIP_TYPES = {"socket", "fifo"}
NICE_LEVEL = 10


def try_nice():
    try:
        os.nice(NICE_LEVEL)
    except OSError:
        pass


def walk_dir(root: Path):
    """Yield (relpath, size, mtime_ns, mode, kind) for everything under root."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        yield ("STATFAIL", str(entry.path), repr(exc), None, None)
                        continue
                    kind = "dir" if entry.is_dir(follow_symlinks=False) else (
                        "symlink" if entry.is_symlink() else (
                            "socket" if stat.S_ISSOCK(st.st_mode) else (
                                "fifo" if stat.S_ISFIFO(st.st_mode) else "file")))
                    if kind == "dir":
                        stack.append(Path(entry.path))
                    else:
                        yield (str(Path(entry.path).relative_to(STORE)),
                               st.st_size, st.st_mtime_ns, st.st_mode, kind)
        except OSError as exc:
            yield ("STATFAIL", str(current), repr(exc), None, None)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def identity_hash(relpath: str, size: int, mtime_ns: int) -> str:
    return hashlib.sha256(f"{relpath}|{size}|{mtime_ns}".encode()).hexdigest()


def process_dir(args):
    """Worker: full stats + (maybe) hashes for one top-level directory."""
    try_nice()
    top, = args
    root = STORE / top
    files, total_bytes = [], 0
    zero_byte = sockets = fifos = symlinks = stat_fails = 0
    odd_perms = []
    for relpath, size, mtime_ns, mode, kind in walk_dir(root):
        if relpath == "STATFAIL":
            stat_fails += 1
            continue
        if kind == "socket":
            sockets += 1
            continue
        if kind == "fifo":
            fifos += 1
            continue
        if kind == "symlink":
            symlinks += 1
            continue
        # kind == file
        if size == 0:
            zero_byte += 1
        if mode is not None and (mode & 0o777) not in (0o644, 0o664, 0o600, 0o755, 0o640, 0o444, 0o400):
            if len(odd_perms) < 20:
                odd_perms.append({"path": relpath, "mode": oct(mode & 0o777)})
        files.append((relpath, size, mtime_ns))
        total_bytes += size

    full_hash = total_bytes <= SMALL_DIR_BYTES
    entries = []
    hashed = 0
    for relpath, size, mtime_ns in files:
        if relpath in SKIP_NAMES:
            continue
        if full_hash and size > 0:
            try:
                entry_hash = file_sha256(STORE / relpath)
                hashed += 1
            except OSError:
                entry_hash = identity_hash(relpath, size, mtime_ns)
        else:
            entry_hash = identity_hash(relpath, size, mtime_ns)
        entries.append(f"{relpath} {size} {entry_hash}")
    entries.sort()
    aggregate = hashlib.sha256("\n".join(entries).encode()).hexdigest() if entries else None
    return {
        "top_level": top,
        "files": len(files),
        "bytes": total_bytes,
        "aggregate_sha256": aggregate,
        "entry_hash_method": "full-content-sha256" if full_hash else "identity(relpath|size|mtime_ns)",
        "zero_byte_files": zero_byte,
        "sockets": sockets,
        "fifos": fifos,
        "symlinks": symlinks,
        "stat_failures": stat_fails,
        "odd_permission_examples": odd_perms,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    global SKIP_NAMES
    SKIP_NAMES = {f"corpus-manifest-{args.date}.json", f"corpus-manifest-{args.date}.md"}

    tops, symlink_targets = [], {}
    for p in sorted(STORE.iterdir(), key=lambda x: x.name):
        if p.is_symlink():
            try:
                target = p.resolve(strict=True)
            except OSError:
                print(f"  WARNING: dangling symlink at root, not walked: {p}",
                      file=sys.stderr, flush=True)
                continue
            if target.is_dir():
                tops.append(p.name)
                symlink_targets[p.name] = str(target)
                print(f"  note: following symlinked top-level dir {p.name} -> {target}",
                      flush=True)
        elif p.is_dir():
            tops.append(p.name)
    loose = sorted(
        str(p.relative_to(STORE)) for p in STORE.iterdir() if p.is_file()
    )
    try_nice()

    results = []
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(process_dir, [(t,) for t in tops]):
            if result["top_level"] in symlink_targets:
                result["symlink_target"] = symlink_targets[result["top_level"]]
            results.append(result)
            print(f"  done: {result['top_level']} ({result['files']} files, "
                  f"{result['bytes']:,} bytes)", flush=True)

    total_files = sum(r["files"] for r in results)
    total_bytes = sum(r["bytes"] for r in results)
    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store": str(STORE),
        "total_files": total_files,
        "total_bytes": total_bytes,
        "top_level_directories": len(results),
        "loose_files_at_root": loose,
        "method": {
            "aggregate": "sha256 over sorted '<relpath> <size> <entry-hash>' lines, per top-level directory",
            "entry_hash": "full per-file content sha256 for directories <= 4 GiB; identity(relpath|size|mtime_ns) sha256 for larger directories",
            "excluded": list(SKIP_NAMES) + ["(nothing else excluded)"],
            "symlinked_top_level": {k: v for k, v in sorted(symlink_targets.items())},
            "noted_separately": "sockets, fifos, zero-byte files, symlinks, stat failures, odd permissions",
            "parallelism": f"{WORKERS} workers, nice {NICE_LEVEL}",
            "caveat": "Store had concurrent scraper/rsync writes while manifesting; re-run after transfers finish for a stable baseline.",
        },
        "directories": sorted(results, key=lambda r: -r["bytes"]),
    }
    out_json = STORE / f"corpus-manifest-{args.date}.json"
    out_json.write_text(json.dumps(manifest, indent=1))

    md = [
        f"# FlossWare corpus manifest — {args.date}",
        "",
        f"- **Files:** {total_files:,}",
        f"- **Bytes:** {total_bytes:,} ({total_bytes/1024**3:.1f} GiB)",
        f"- **Top-level dirs:** {len(results)}; loose root files: {len(loose)}",
        "",
        "| Directory | Files | Bytes | Hash method | Anomalies |",
        "|---|---:|---:|---|---|",
    ]
    for r in manifest["directories"]:
        anomalies = []
        if r["zero_byte_files"]:
            anomalies.append(f"{r['zero_byte_files']} zero-byte")
        if r["stat_failures"]:
            anomalies.append(f"{r['stat_failures']} stat-fails")
        if r["sockets"] or r["fifos"]:
            anomalies.append(f"{r['sockets']} sockets/{r['fifos']} fifos")
        name = r["top_level"]
        if "symlink_target" in r:
            name += f" (symlink → {r['symlink_target']})"
        md.append(
            f"| {name} | {r['files']:,} | {r['bytes']:,} | "
            f"{'full' if r['entry_hash_method'].startswith('full') else 'identity'} "
            f"| {', '.join(anomalies) or '-'} |")
    (STORE / f"corpus-manifest-{args.date}.md").write_text("\n".join(md) + "\n")

    print(f"\nMANIFEST DONE: {total_files:,} files, {total_bytes:,} bytes")
    print(f"  json: {out_json}")
    print(f"  md:   {STORE / f'corpus-manifest-{args.date}.md'}")


if __name__ == "__main__":
    main()
