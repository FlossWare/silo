# silo-query service contract (v1)

Read-only query service over the artifacts produced by silo's batch
tools. Thin by design: the batch tools (run via timers/finishers) walk
the store and write artifacts; this service loads those artifacts and
answers cheap queries. It never walks the store itself.

Normative language follows the FlossWare standards ADR conventions
(SHALL/SHOULD/MAY).

## 1. Identity and deployment

- systemd user unit: `flossware-silo-query.service` (ADR-0034 §3)
- REST: HTTP/1.1 bound to `127.0.0.1` only, default port `8791`
  (`SILO_QUERY_PORT`; the default SHALL be verified free at deploy —
  8766–8790 is the CE/memory range)
- Socket: `$XDG_RUNTIME_DIR/flossware/silo.sock` (ADR-0034 §4); same
  API over both transports
- The service SHALL NOT accept connections from non-loopback
  interfaces in v1

## 2. Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `SILO_ROOT` | `~/corpus` | store root; corpus-manifest baselines live here |
| `SILO_URL_MASTER` | `$SILO_ROOT/url-master.jsonl` | URL master index |
| `SILO_PDF_SNAPSHOTS` | `$SILO_ROOT/pdf-snapshots/` | dated pdf-manifest archive (see §6) |

All artifact paths are operator-configurable; the service reports
exactly what it loaded (§4 `/ready`) and never guesses.

## 3. Artifacts and loading

The service reads, at startup and on mtime change (hot reload):

1. **URL master** (`url-master.jsonl`) — one record per unique URL with
   provenance. Loaded into memory (URL-keyed dict). A reload failure
   SHALL keep the last-good copy loaded and flag staleness.
2. **Corpus manifest baselines** (`corpus-manifest-<date>.json`) —
   per-directory aggregate baselines. Only the index of available
   dates is held in memory; a specific baseline is loaded on demand
   and cached.
3. **PDF manifest snapshots** (`pdf-snapshots/pdf-manifest-<date>.jsonl`)
   — per-PDF records. The service reads ONLY the snapshots directory;
   the batch tool's live output path is never read. "Latest" means the
   newest dated file by lexicographic ISO-date filename order. Summary
   of the latest snapshot held in memory; per-PDF records read on
   demand for diffs.

A missing artifact is NOT a startup failure: the service starts, /ready
reports the artifact as `missing`, and the endpoints depending on it
return `503 artifact-missing` (§5). Endpoints not depending on it keep
working. This is the honest-unavailable rule: no endpoint SHALL
silently return empty results for a missing artifact.

A CORRUPT or unparseable artifact (truncated baseline, invalid JSON,
mid-write partial file) is reported as `503 artifact-corrupt` (§5) by
the endpoints that would read it. The service SHALL NOT return partial
results from a partially-read artifact: the whole artifact is loaded or
the error is returned. For the URL master, a failed reload keeps the
last-good copy (flagged stale); for on-demand artifacts (baselines,
snapshots), a read failure is a per-request `503 artifact-corrupt`.

## 4. Endpoints

### GET /health
Liveness. Always `200 {"ok": true}` when the process is up. Carries no
dependency information.

### GET /ready
Readiness and artifact inventory:

```json
{
  "ok": true,
  "artifacts": {
    "url_master":   {"loaded": true, "urls": 1128462, "mtime": "2026-11-25T08:34:00Z", "stale": false},
    "manifests":    {"loaded": true, "dates": ["2026-11-21", "2026-11-23"]},
    "pdf_snapshots":{"loaded": false, "missing": true}
  }
}
```

`ok` is `false` when any artifact is missing or stale; the service
still serves endpoints whose artifacts are healthy.

### GET /v1/urls?url=<url>
URL existence and provenance from the master index.

- `200` — `{"exists": true, "url": "...", "provenance": ["web-scrape-corpus"], "title": "...", "category": "...", "fetched_at": "...", "last_checked_at": "..."}` (fields other than `exists`/`url`/`provenance` present only when known)
- `200` — `{"exists": false, "url": "..."}` when the URL is simply absent
- `400` — url missing, not `http`/`https`, or longer than 2048 bytes
- `503` — url master not loaded (§3)

The `url` parameter SHALL be matched exactly (no normalization beyond
byte-exact comparison in v1).

### GET /v1/manifests
Available baseline dates: `200 {"dates": ["2026-11-21", "2026-11-23"]}`.

### GET /v1/manifests/{date}
Summary of one baseline: totals plus per-directory entries as recorded
by `corpus_manifest.py` (files, bytes, hash method, anomalies,
symlinked targets). `404` for an unknown date; `400` for a date not
matching `YYYY-MM-DD`.

### GET /v1/manifests/{date}/diff/{date2}
Directory-level diff between two baselines: directories added/removed,
and per-directory file-count/byte deltas with an `aggregate_changed`
flag. `{date2}` is the BASELINE (earlier state); `added`/`removed` and
deltas are relative to it. `400` for dates not matching `YYYY-MM-DD` or
when `{date}` is earlier than `{date2}`; `404` for either date unknown;
an equal-date pair returns an empty diff. **Granularity is per
top-level directory** — the baselines are aggregate hashes, not
per-file lists. Per-file change detection is the pdf-snapshot diff
(§ below) and url master's domain; this endpoint SHALL NOT imply
file-level precision it does not have.

### GET /v1/pdfs
Summary of the latest PDF snapshot: total PDFs, magic-check failures,
unreadable count, snapshot date.

### GET /v1/pdfs/diff/{date}
Per-PDF diff of the latest snapshot against the dated snapshot:
`added`, `removed`, `changed` (sha256 or size/mtime differ) path lists.
Responses are bounded: at most 10,000 total entries, with
`{"truncated": true, "totals": {...}}` when exceeded; totals are always
complete. `404` when the dated snapshot does not exist.

## 5. Response envelope

Every `/v1/*` response carries a `meta` block identifying the artifact
revision that answered it:

```json
{"meta": {"artifact": "url_master", "mtime": "2026-11-25T08:34:00Z", "stale": false}, ...}
```

Answers SHALL NOT claim freshness beyond the artifact's mtime. Errors
use a single envelope: `{"error": "<code>", "detail": "..."}` with
codes `bad-request` (400), `not-found` (404), `artifact-missing` (503),
`artifact-corrupt` (503).

## 6. Snapshot retention convention (deployment)

`pdf-manifest.py` writes one atomic output path. For diff support, the
timer/finisher that runs it SHALL additionally retain a dated copy:
`$SILO_PDF_SNAPSHOTS/pdf-manifest-YYYY-MM-DD.jsonl`. Retained copies
SHALL be written to a temporary name and atomically renamed into place
(never written in-place), so the service never observes a partial
snapshot. This is a deployment convention (the tool's
`SILO_PDF_MANIFEST` env already supports arbitrary output paths), not a
tool change.

## 7. Non-goals (v1)

- No writes: no POST/PUT/DELETE — the service owns no state
- No authentication/RBAC: single-user workstation, loopback + unix
  socket only; multi-user concerns are out of scope
- No store walks, no hashing, no URL normalization, no full-text
  search (that is the ingest/index domain, not silo's)
- No CE gateway integration: CE services register through their own
  gateway; a bridge is a future, separately-reviewed contract
- No batching in v1: single-URL lookup only; a batch endpoint is v2
  material once a consumer needs it

## 8. Versioning

Endpoint paths are versioned (`/v1/`). Additive changes (new optional
fields, new endpoints) MAY ship within v1; breaking changes require
`/v2` and a new contract document. This follows
[ADR-0031](https://github.com/FlossWare/standards/blob/main/adr/ADR-0031-rest-representation-contracts-and-api-evolution.md).
