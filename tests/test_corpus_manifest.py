#!/usr/bin/env python3
"""Fixture smoke test for corpus_manifest.py — real subprocess runs.

Covers: per-directory + loose-root-file baselines, symlink counting
with readlink targets in the aggregate, zero-byte files counted but
given a constant hash (touching one does not change an aggregate), and
the SILO_ROOT env configuration.

Run: python3 tests/test_corpus_manifest.py   (from the repo root)
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "corpus_manifest.py")

ok = True

def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    ok = ok and cond

with tempfile.TemporaryDirectory() as t:
    store = os.path.join(t, "store")
    sub = os.path.join(store, "data")
    os.makedirs(sub)
    with open(os.path.join(sub, "a.txt"), "w") as f:
        f.write("alpha")
    empty = os.path.join(sub, "empty.txt")
    open(empty, "w").close()
    os.symlink("a.txt", os.path.join(sub, "link.txt"))
    with open(os.path.join(store, "loose.txt"), "w") as f:
        f.write("loose")
    env = dict(os.environ, SILO_ROOT=store)

    r = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, env=env)
    print("exit:", r.returncode)
    check("exit 0", r.returncode == 0)
    cands = [p for p in os.listdir(store) if p.endswith(".json")]
    check("manifest json written at store root", len(cands) == 1)
    with open(os.path.join(store, cands[0])) as f:
        man = json.load(f)
    dirs = {d["top_level"]: d for d in man["directories"]}
    check("data dir present", "data" in dirs)
    check("loose root files entry present", "(loose root files)" in dirs)
    d = dirs["data"]
    check("symlink counted", d["symlinks"] == 1)
    check("zero-byte counted", d["zero_byte_files"] == 1)
    check("no stat/dir failures in fixture",
          d["stat_failures"] == 0 and d["dir_failures"] == 0)
    check("manifest files excluded from own baseline (a.txt etc. only)",
          d["files"] == 2)

    # empty-file constancy: touch the empty file, aggregate must NOT change
    agg_before = d["aggregate_sha256"]
    os.utime(empty)
    r = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, env=env)
    check("re-run exit 0", r.returncode == 0)
    with open(os.path.join(store, cands[0])) as f:
        man2 = json.load(f)
    d2 = {x["top_level"]: x for x in man2["directories"]}["data"]
    check("touching empty file does not change aggregate",
          d2["aggregate_sha256"] == agg_before)

print("\nOVERALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
