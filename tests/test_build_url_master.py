#!/usr/bin/env python3
"""Fixture smoke test for build-url-master.py — real subprocess runs.

Covers: JSONL multi-record extraction, per-line strict UTF-8 decode
(skip+count, never corrupt), seed drop warnings, provenance type guard,
realpath exclusion of master/seeds, existing-master merge, and the
fatal-on-unparseable-master-line rule (no lossy rewrite).

Run: python3 tests/test_build_url_master.py   (from the repo root)
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "build-url-master.py")

ok = True

def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    ok = ok and cond

with tempfile.TemporaryDirectory() as t:
    scan = os.path.join(t, "scan", "sub")
    os.makedirs(scan)
    # multi-record jsonl
    with open(os.path.join(t, "scan", "one.jsonl"), "w") as f:
        f.write('{"url":"https://a.example/1","title":"A1"}\n'
                '{"url":"https://a.example/2","title":"A2"}\n')
    # nested jsonl
    with open(os.path.join(scan, "two.jsonl"), "w") as f:
        f.write('{"url":"https://b.example/ok"}\n')
    # invalid-utf8 line (must be skipped+counted, not corrupted into a URL)
    with open(os.path.join(t, "scan", "bad.jsonl"), "wb") as f:
        f.write(b'{"url":"https://c.example/\xff\xfe-badbytes"}\n')
    # plain single-record json
    with open(os.path.join(t, "scan", "three.json"), "w") as f:
        f.write('{"url":"https://d.example/1","category":"docs"}')
    # pre-existing master
    master = os.path.join(t, "master.jsonl")
    with open(master, "w") as f:
        f.write('{"url":"https://master.example/keep","provenance":["existing-master"]}\n')
    # seed: valid + malformed + non-dict + no-url + dict-provenance
    seed = os.path.join(t, "seed.jsonl")
    with open(seed, "w") as f:
        f.write('{"url":"https://seed.example/1"}\n'
                'not json at all\n'
                '[1,2,3]\n'
                '{"no_url":true}\n'
                '{"url":"https://seed.example/2","provenance":{"evil":"dict"}}\n')
    # master reachable through a symlink inside scan root (realpath exclusion)
    os.symlink(master, os.path.join(t, "scan", "master-link.jsonl"))

    r = subprocess.run([sys.executable, TOOL, master, os.path.join(t, "scan"), seed],
                       capture_output=True, text=True)
    print("exit:", r.returncode, "|", r.stdout.strip())

    rows = {}
    with open(master) as f:
        for line in f:
            rec = json.loads(line)
            rows[rec["url"]] = rec

    expected = {"https://a.example/1", "https://a.example/2",
                "https://b.example/ok", "https://d.example/1",
                "https://seed.example/1", "https://seed.example/2",
                "https://master.example/keep"}
    check("all 7 expected URLs present", set(rows) == expected)
    check("invalid-utf8 URL NOT present (skipped, not corrupted)",
          not any("�" in u for u in rows))
    check("bad-utf8 line counted as skipped", "lines=1" in r.stdout)
    check("seed drops warned (3 malformed lines)",
          "3 unparseable line(s) dropped from seed" in r.stderr)
    check("dict provenance did not crash and did not leak keys",
          rows["https://seed.example/2"]["provenance"] == ["seed-file"])
    check("symlinked master NOT rescanned (keep has no web-scrape-corpus tag)",
          "web-scrape-corpus" not in rows["https://master.example/keep"]["provenance"])
    check("scanned URLs tagged web-scrape-corpus",
          "web-scrape-corpus" in rows["https://a.example/1"]["provenance"])
    check("existing master merged (kept URL)", "https://master.example/keep" in rows)

    # fatal-on-master-loss: corrupt the master, re-run
    with open(master, "a") as f:
        f.write("this line is not json\n")
    r2 = subprocess.run([sys.executable, TOOL, master, os.path.join(t, "scan")],
                        capture_output=True, text=True)
    check("unparseable master line is FATAL (exit != 0)", r2.returncode != 0)
    check("fatal message names the master", "existing master" in (r2.stderr + r2.stdout))
    with open(master) as f:
        content = f.read()
    check("master NOT rewritten after fatal (line still there)",
          "this line is not json" in content)

print("\nOVERALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
