#!/usr/bin/env python3
"""Fixture smoke test for pdf-manifest.py — real subprocess runs.

Covers: %PDF magic check, sha256 matching sha256sum (the seek(0)-before-
file_digest fix), fstat-on-open-fd, symlinked-dir descent, dangling
.pdf symlinks producing error records, and the missing-root abort that
never replaces an existing manifest.

Run: python3 tests/test_pdf_manifest.py   (from the repo root)
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "pdf-manifest.py")

ok = True

def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    ok = ok and cond

with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "store")
    os.makedirs(root)
    # a real small PDF (magic ok)
    good = os.path.join(root, "good.pdf")
    payload = b"%PDF-1.4 hello world" + b"x" * 100
    with open(good, "wb") as f:
        f.write(payload)
    # a text file named .pdf (magic fail)
    with open(os.path.join(root, "fake.pdf"), "w") as f:
        f.write("not a pdf at all")
    # a dangling .pdf symlink (must produce an error record, not vanish)
    os.symlink(os.path.join(t, "nowhere.pdf"), os.path.join(root, "dangling.pdf"))
    # a symlinked subdir (must be descended into)
    other = os.path.join(t, "other")
    os.makedirs(other)
    with open(os.path.join(other, "nested.pdf"), "wb") as f:
        f.write(b"%PDF-1.7 nested")
    os.symlink(other, os.path.join(root, "linkdir"))
    out = os.path.join(t, "pdf-manifest.jsonl")

    env = dict(os.environ, SILO_PDF_ROOTS=root, SILO_PDF_MANIFEST=out)
    r = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, env=env)
    print("exit:", r.returncode, "|", r.stdout.strip())
    check("exit 0", r.returncode == 0)
    recs = {}
    for line in open(out):
        rec = json.loads(line)
        recs[rec["path"]] = rec
    check("3 PDFs + dangling recorded (4 records)", len(recs) == 4)
    check("nested PDF found through symlinked dir",
          any(p.endswith("nested.pdf") for p in recs))
    check("dangling symlink produced error record",
          any("dangling.pdf" in p and "error" in recs[p] for p in recs))
    check("good.pdf magic_ok", recs[good]["magic_ok"] is True)
    check("fake.pdf magic_ok False",
          any("fake.pdf" in p and recs[p]["magic_ok"] is False for p in recs))
    with open(good, "rb") as f:
        expected = hashlib.sha256(f.read()).hexdigest()
    check("good.pdf sha256 matches sha256sum (seek fix)",
          recs[good]["sha256"] == expected)

    # missing root must be FATAL and must NOT replace the manifest
    before = open(out).read()
    env2 = dict(os.environ, SILO_PDF_ROOTS=os.path.join(t, "missing-root"),
                SILO_PDF_MANIFEST=out)
    r2 = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, env=env2)
    check("missing root is fatal (exit != 0)", r2.returncode != 0)
    check("manifest NOT replaced on fatal", open(out).read() == before)

print("\nOVERALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
