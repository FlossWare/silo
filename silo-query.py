#!/usr/bin/env python3
"""silo-query — read-only query service over the store's artifacts.

Implements docs/contracts/silo-query.md (v1): REST on 127.0.0.1 plus a
unix socket, honest-unavailable semantics, mtime hot-reload, bounded
responses. Never walks the store; only reads the artifacts produced by
the batch tools (url-master, corpus manifests, pdf snapshots).

Config (env):
  SILO_ROOT            store root (default ~/corpus)
  SILO_URL_MASTER      url-master.jsonl path
  SILO_PDF_SNAPSHOTS   dated pdf snapshot dir
  SILO_QUERY_PORT      REST port (default 8791)
  SILO_QUERY_SOCKET    socket path (default $XDG_RUNTIME_DIR/flossware/silo.sock)

Requires Python >= 3.9. Stdlib only.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SILO_ROOT = Path(os.environ.get("SILO_ROOT") or Path.home() / "corpus")
URL_MASTER = Path(os.environ.get("SILO_URL_MASTER") or SILO_ROOT / "catalog/url-master.jsonl")
PDF_SNAPSHOTS = Path(os.environ.get("SILO_PDF_SNAPSHOTS") or SILO_ROOT / "pdf-snapshots")
PORT = int(os.environ.get("SILO_QUERY_PORT") or "8791")
SOCKET = os.environ.get("SILO_QUERY_SOCKET") or \
    str(Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "flossware/silo.sock")

MAX_URL_BYTES = 2048
MAX_DIFF_ENTRIES = 10000
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
# master/seed rows may carry non-http schemes (concept:// etc.)
URL_VALID = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


class UrlMaster:
    """URL-keyed index with mtime hot-reload and last-good fallback.

    Reload invariants (CE-reviewed): lookups keep serving the last-good
    rows for the entire duration of a reload — state changes only at
    the atomic swap, never to a transient "loading"; a failed reload
    keeps last-good rows with an error flag and records the attempted
    mtime so it does not retry on every request; the mtime is taken
    from the open fd (fstat) so it describes the data actually read.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.reload_lock = threading.Lock()
        self.rows = {}          # url -> record
        self.mtime = None
        self.state = "missing"  # missing | corrupt | loaded
        self.error = None

    def load(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            with self.lock:
                if not self.rows:
                    self.state = "missing"
                else:
                    self.error = "master file vanished; serving last-good"
            return
        self._load(m)

    def maybe_reload(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        with self.lock:
            if m == self.mtime:
                return
        with self.reload_lock:          # one reloader at a time
            with self.lock:
                if m == self.mtime:     # re-check: another thread reloaded
                    return
            self._load(m)

    def _load(self, attempted_mtime):
        rows = {}
        dropped = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                mtime = os.fstat(f.fileno()).st_mtime
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
                    if not isinstance(u, str) or not URL_VALID.match(u):
                        dropped += 1
                        continue
                    rows[u] = r
        except FileNotFoundError:
            with self.lock:
                if not self.rows:
                    self.state = "missing"
                else:
                    self.error = "master file vanished; serving last-good"
                    self.mtime = attempted_mtime
            return
        except OSError as exc:
            with self.lock:
                if not self.rows:
                    self.state = "corrupt"
                self.error = repr(exc)
                self.mtime = attempted_mtime   # no retry storm
            return
        with self.lock:
            self.rows = rows          # atomic last-good swap; never partial
            self.mtime = mtime
            self.state = "loaded"
            self.error = f"dropped {dropped} malformed line(s)" if dropped else None

    def lookup(self, url):
        with self.lock:
            if self.state != "loaded":
                return None, self.state
            return self.rows.get(url), "loaded"


MASTER = UrlMaster(URL_MASTER)
MANIFEST_CACHE = {}       # date -> (mtime, data)
CACHE_LOCK = threading.Lock()


MANIFEST_NAME_RE = re.compile(r"corpus-manifest-(\d{4}-\d{2}-\d{2})\.json")
SNAPSHOT_NAME_RE = re.compile(r"pdf-manifest-(\d{4}-\d{2}-\d{2})\.jsonl")


def manifest_dates():
    out = []
    try:
        for p in SILO_ROOT.iterdir():
            m = MANIFEST_NAME_RE.fullmatch(p.name)
            if m:
                out.append(m.group(1))
    except OSError:
        pass
    return sorted(set(out))


def load_manifest(date):
    if not DATE_RE.fullmatch(date):
        return None
    path = SILO_ROOT / f"corpus-manifest-{date}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with CACHE_LOCK:
        hit = MANIFEST_CACHE.get(date)
        if hit and hit[0] == mtime:
            return hit[1]           # may be a cached "corrupt" failure
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = "corrupt"
    with CACHE_LOCK:
        MANIFEST_CACHE[date] = (mtime, data)
    return data


def snapshot_dates():
    try:
        return sorted(m.group(1) for m in
                      (SNAPSHOT_NAME_RE.fullmatch(p.name)
                       for p in PDF_SNAPSHOTS.iterdir()) if m)
    except OSError:
        return []


def pdf_summary(path):
    """Count totals for one snapshot file. Cached by mtime via caller."""
    total = bad_magic = errors = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    errors += 1
                    continue
                if not isinstance(rec, dict):
                    errors += 1
                    continue
                if "error" in rec:
                    errors += 1
                else:
                    total += 1
                    bad_magic += not rec.get("magic_ok", False)
    except OSError:
        return None
    return {"pdfs": total, "magic_failures": bad_magic, "unreadable": errors,
            "snapshot": path.stem.replace("pdf-manifest-", "")}


def pdf_records(path):
    """path -> (sha256, size, mtime) for diffing."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if "error" not in rec and "sha256" in rec:
                    out[rec["path"]] = (rec["sha256"], rec.get("size"), rec.get("mtime"))
    except OSError:
        return None
    return out


def diff_days(date, date2):
    a, b = load_manifest(date), load_manifest(date2)
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    da = {d["top_level"]: d for d in a.get("directories", [])}
    db = {d["top_level"]: d for d in b.get("directories", [])}
    added = sorted(set(da) - set(db))
    removed = sorted(set(db) - set(da))
    changed = {}
    for k in sorted(set(da) & set(db)):
        x, y = da[k], db[k]
        if x.get("aggregate_sha256") != y.get("aggregate_sha256"):
            changed[k] = {"files": x.get("files", 0) - y.get("files", 0),
                          "bytes": x.get("bytes", 0) - y.get("bytes", 0),
                          "aggregate_changed": True}
    return {"baseline": date2, "compared": date,
            "directories_added": added, "directories_removed": removed,
            "directories_changed": changed,
            "totals": {"files": a.get("total_files", 0) - b.get("total_files", 0),
                       "bytes": a.get("total_bytes", 0) - b.get("total_bytes", 0)}}


def pdf_diff(date):
    dates = snapshot_dates()
    if not dates or date not in dates:
        return None
    latest = dates[-1]
    if date == latest:
        return {"added": [], "removed": [], "changed": [], "truncated": False,
                "totals": {"added": 0, "removed": 0, "changed": 0},
                "baseline": date, "compared": latest}
    new = pdf_records(PDF_SNAPSHOTS / f"pdf-manifest-{latest}.jsonl")
    old = pdf_records(PDF_SNAPSHOTS / f"pdf-manifest-{date}.jsonl")
    if new is None or old is None:
        return "corrupt"
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(new) & set(old) if new[p] != old[p])
    truncated = (len(added) + len(removed) + len(changed)) > MAX_DIFF_ENTRIES
    return {"added": added[:MAX_DIFF_ENTRIES], "removed": removed[:MAX_DIFF_ENTRIES],
            "changed": changed[:MAX_DIFF_ENTRIES], "truncated": truncated,
            "totals": {"added": len(added), "removed": len(removed),
                       "changed": len(changed)},
            "baseline": date, "compared": latest}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "silo-query/1.0"

    def log_message(self, fmt, *args):
        # unix-socket clients have an EMPTY client_address (unnamed),
        # so address_string()'s client_address[0] raises IndexError —
        # never let logging kill a response
        try:
            peer = self.client_address[0]
        except (IndexError, TypeError):
            peer = "unix"
        sys.stderr.write(f"{peer} {fmt % args}\n")

    # ---- helpers ----
    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, err, detail=""):
        self.send_json(code, {"error": err, "detail": detail})

    def meta(self, artifact, mtime, stale=False):
        return {"meta": {"artifact": artifact, "mtime": mtime, "stale": stale}}

    # ---- routing ----
    def do_GET(self):
        parts = [p for p in self.path.split("?")]
        path = urllib.parse.unquote(parts[0]).rstrip("/") or "/"
        query = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}

        if path == "/health":
            return self.send_json(200, {"ok": True})
        if path == "/ready":
            return self.ready()
        if path == "/v1/urls":
            return self.urls(query)
        if path == "/v1/manifests":
            return self.manifests()
        m = re.fullmatch(r"/v1/manifests/(\d{4}-\d{2}-\d{2})", path)
        if m:
            return self.manifest(m.group(1))
        m = re.fullmatch(r"/v1/manifests/(\d{4}-\d{2}-\d{2})/diff/(\d{4}-\d{2}-\d{2})", path)
        if m:
            return self.manifest_diff(m.group(1), m.group(2))
        if path == "/v1/pdfs":
            return self.pdfs()
        m = re.fullmatch(r"/v1/pdfs/diff/(\d{4}-\d{2}-\d{2})", path)
        if m:
            return self.pdfs_diff(m.group(1))
        self.error(404, "not-found", f"unknown path {path}")

    # ---- endpoints ----
    def ready(self):
        MASTER.maybe_reload()
        dates = manifest_dates()
        snaps = snapshot_dates()
        um = {"loaded": MASTER.state == "loaded", "state": MASTER.state}
        if MASTER.state == "loaded":
            um["urls"] = len(MASTER.rows)
            um["mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(MASTER.mtime))
            um["stale"] = bool(MASTER.error)
            if MASTER.error:
                um["note"] = MASTER.error
        out = {"ok": MASTER.state == "loaded" and bool(dates or snaps),
               "artifacts": {"url_master": um,
                             "manifests": {"loaded": bool(dates), "dates": dates},
                             "pdf_snapshots": {"loaded": bool(snaps), "dates": snaps}}}
        self.send_json(200, out)

    def urls(self, query):
        MASTER.maybe_reload()
        if MASTER.state != "loaded":
            return self.error(503, "artifact-missing" if MASTER.state == "missing"
                              else "artifact-corrupt",
                              f"url master {MASTER.state}" +
                              (f": {MASTER.error}" if MASTER.error else ""))
        values = query.get("url", [])
        if not values:
            return self.error(400, "bad-request", "url parameter required")
        url = values[0]
        if len(url.encode("utf-8", "surrogatepass")) > MAX_URL_BYTES:
            return self.error(400, "bad-request", f"url exceeds {MAX_URL_BYTES} bytes")
        if not URL_VALID.match(url):
            return self.error(400, "bad-request", "url must carry a scheme (e.g. https://)")
        rec, _ = MASTER.lookup(url)
        out = self.meta("url_master",
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(MASTER.mtime or 0)),
                        stale=bool(MASTER.error))
        if rec is None:
            out.update({"exists": False, "url": url})
        else:
            out.update({"exists": True, **{k: rec[k] for k in
                         ("url", "provenance", "title", "category", "fetched_at",
                          "last_checked_at") if rec.get(k) is not None}})
        self.send_json(200, out)

    def manifests(self):
        dates = manifest_dates()
        self.send_json(200, {**self.meta("manifest-index", None), "dates": dates})

    def manifest(self, date):
        data = load_manifest(date)
        if data is None:
            return self.error(404, "not-found", f"no baseline for {date}")
        if data == "corrupt":
            return self.error(503, "artifact-corrupt", f"baseline {date} unparseable")
        self.send_json(200, data)

    def manifest_diff(self, date, date2):
        if not DATE_RE.fullmatch(date) or not DATE_RE.fullmatch(date2):
            return self.error(400, "bad-request", "dates must be YYYY-MM-DD")
        if date < date2:
            return self.error(400, "bad-request",
                              f"{date} is earlier than baseline {date2}")
        if date == date2:
            return self.send_json(200, {"baseline": date2, "compared": date,
                                        "directories_added": [],
                                        "directories_removed": [],
                                        "directories_changed": {},
                                        "totals": {"files": 0, "bytes": 0}})
        if date not in manifest_dates() or date2 not in manifest_dates():
            return self.error(404, "not-found", "unknown baseline date")
        result = diff_days(date, date2)
        if result is None:
            return self.error(503, "artifact-corrupt", "baseline unparseable")
        self.send_json(200, result)

    def pdfs(self):
        dates = snapshot_dates()
        if not dates:
            return self.error(503, "artifact-missing", "no pdf snapshots")
        latest = PDF_SNAPSHOTS / f"pdf-manifest-{dates[-1]}.jsonl"
        summary = pdf_summary(latest)
        if summary is None:
            return self.error(503, "artifact-corrupt", f"snapshot {dates[-1]} unreadable")
        self.send_json(200, summary)

    def pdfs_diff(self, date):
        if not DATE_RE.fullmatch(date):
            return self.error(400, "bad-request", "date must be YYYY-MM-DD")
        result = pdf_diff(date)
        if result is None:
            return self.error(404, "not-found", f"no snapshot for {date}")
        if result == "corrupt":
            return self.error(503, "artifact-corrupt", "snapshot unparseable")
        self.send_json(200, result)


def main():
    MASTER.load()
    threading.Thread(target=_reload_loop, daemon=True).start()

    tcp = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    sock_dir = os.path.dirname(SOCKET)
    if sock_dir:
        os.makedirs(sock_dir, exist_ok=True)
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    import socket
    import socketserver
    unix = socketserver.ThreadingUnixStreamServer(SOCKET, Handler)
    os.chmod(SOCKET, 0o600)

    print(f"silo-query: REST on 127.0.0.1:{PORT}, socket {SOCKET}", flush=True)
    print(f"  url_master: {URL_MASTER} ({MASTER.state}, {len(MASTER.rows)} urls)", flush=True)
    print(f"  manifests: {manifest_dates()}", flush=True)
    print(f"  pdf snapshots: {snapshot_dates()}", flush=True)

    threading.Thread(target=unix.serve_forever, daemon=True).start()
    tcp.serve_forever()


def _reload_loop():
    while True:
        time.sleep(30)
        MASTER.maybe_reload()


if __name__ == "__main__":
    main()
