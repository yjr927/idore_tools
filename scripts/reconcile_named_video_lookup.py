"""Read-only evidence for the two 2026-11-23 New York named video reservations."""
import hashlib
import http.client
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta


BASE = "/api/v1"
MODEL = "bytedance/seedance-2.5"
DAY = "2026-11-24T01:35:"
JOB = re.compile(r"gen-vid-([0-9]{10})-[A-Za-z0-9]{20}", re.ASCII)
MAX_BODY = 256 * 1024
LIMIT = 8


@dataclass(frozen=True)
class Slot:
    name: str
    start: str
    end: str
    reserved: str
    stopped: str
    frame_digest: str
    prompt_digest: str


# Original main-branch reservation and failure checkpoints (UTC). Digests cover
# the exact approved role first-frame URL and the actual observed-motion prompt.
SLOTS = (
    Slot("bay", "17", "23", "17.305609", "22.322023",
         "fd37d213345f1fb90d674df4ebf6d18fc00ae3e8de293289438643ada781c893",
         "6fe0705e9bc8f6c29e46a77f13c28da40e7f30b8bf457b73a43a3d6364600f03"),
    Slot("momoko", "23", "29", "23.878747", "27.891087",
         "6e2f70ef81ef104b488d1166886cf441374da03641002632be4e8f1520768282",
         "83123fa281fffb58cc36b0152bfaa5c5dd01a0cea5dad47755f8473592895c9b"),
)


class StopLookup(Exception):
    """Any HTTP, schema or transport failure stops the lookup without replay."""


def call(key, label, path, payload=None):
    # HTTPSConnection has no redirects, proxy discovery, cookies or retry logic.
    is_job = path.startswith("/videos/") and JOB.fullmatch(path[8:])
    if not ((payload is None and (path in ("/key", "/analytics/meta") or is_job))
            or (payload is not None and path == "/analytics/query")):
        raise StopLookup
    conn = http.client.HTTPSConnection("openrouter.ai", timeout=12)
    try:
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json",
                   "User-Agent": "idore-video-reconciliation/1.0"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        conn.request("GET" if body is None else "POST", BASE + path, body, headers)
        response = conn.getresponse()
        print(label, "http", response.status)
        if response.status != 200:
            raise StopLookup
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise StopLookup
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise StopLookup
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise StopLookup
        return obj
    finally:
        conn.close()


def names(meta, key):
    return {entry["name"] for entry in meta[key] if isinstance(entry, dict) and isinstance(entry.get("name"), str)}


def rows_for(key, label, start, end):
    # The sole POST is OpenRouter's read-only analytics query.
    response = call(key, label, "/analytics/query", {
        "metrics": ["request_count"], "dimensions": ["generation_id", "model"],
        "filters": [{"field": "model", "operator": "eq", "value": MODEL}],
        "time_range": {"start": DAY + start + "Z", "end": DAY + end + "Z"},
        "limit": LIMIT,
    })["data"]
    rows, meta = response["data"], response["metadata"]
    if (not isinstance(rows, list) or len(rows) >= LIMIT or response.get("warnings")
            or meta.get("truncated") is not False or meta.get("row_count") != len(rows)
            or any(not isinstance(r, dict) or r.get("model") != MODEL
                   or type(r.get("request_count")) is not int or r["request_count"] < 1
                   or not isinstance(r.get("generation_id"), str) for r in rows)):
        raise StopLookup
    return rows


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def verified(job, candidate, slot):
    if (job.get("id") != candidate or job.get("generation_id", candidate) != candidate
            or job.get("model", MODEL) != MODEL or job.get("polling_url") != BASE + "/videos/" + candidate):
        return False
    # Seconds encoded in the official job ID must lie wholly inside the precise
    # git reservation interval. The two whole-second query windows are wider.
    ts = int(JOB.fullmatch(candidate)[1])
    start = datetime.fromisoformat(DAY + slot.reserved + "+00:00")
    end = datetime.fromisoformat(DAY + slot.stopped + "+00:00")
    moment = datetime.fromtimestamp(ts, start.tzinfo)
    if not start <= moment or moment + timedelta(seconds=1) > end:
        return False
    # Current status schema does not promise request inputs. Model and timing on
    # a shared workspace alone cannot prove that a job belongs to this dancer.
    submitted = job.get("request", job)
    if not isinstance(submitted, dict) or digest(submitted.get("prompt")) != slot.prompt_digest:
        return False
    frames = submitted.get("frame_images")
    if not isinstance(frames, list) or len(frames) != 1 or not isinstance(frames[0], dict):
        return False
    frame = frames[0]
    image = frame.get("image_url")
    return (submitted.get("model", MODEL) == MODEL and frame.get("frame_type") == "first_frame"
            and frame.get("type") == "image_url" and isinstance(image, dict)
            and digest(image.get("url")) == slot.frame_digest)


def run(key):
    identity = call(key, "project_key", "/key").get("data", {})
    managed = identity.get("is_management_key")
    print("management_access", managed if type(managed) is bool else "unspecified")
    meta = call(key, "meta", "/analytics/meta")["data"]
    if (not {"generation_id", "model"} <= names(meta, "dimensions")
            or "request_count" not in names(meta, "metrics") or "eq" not in names(meta, "operators")):
        print("schema_unsupported")
        return
    outer = rows_for(key, "window", "15", "29")
    found = {s.name: rows_for(key, s.name, s.start, s.end) for s in SLOTS}
    for slot in SLOTS:
        rows = found[slot.name]
        if not rows:
            print(slot.name, "no_visible_match_inconclusive")
            continue
        if len(rows) != 1 or rows[0]["request_count"] != 1:
            print(slot.name, "ambiguous")
            continue
        candidate = rows[0]["generation_id"]
        if (sum(r["request_count"] for r in outer if r["generation_id"] == candidate) != 1
                or sum(r["generation_id"] == candidate for rr in found.values() for r in rr) != 1):
            print(slot.name, "inconsistent_windows")
            continue
        if not JOB.fullmatch(candidate) or key in candidate:
            print(slot.name, "single_unusable_generation_id")
            continue
        job = call(key, slot.name, "/videos/" + candidate)
        if not verified(job, candidate, slot):
            print(slot.name, "single_video_binding_unconfirmed")
        elif job.get("status") in ("pending", "in_progress", "completed"):
            print(slot.name, "verified_job", candidate)
        else:
            print(slot.name, "confirmed_job_unavailable")


if __name__ == "__main__":
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("missing_key")
    else:
        try:
            run(key)
        except (StopLookup, OSError, http.client.HTTPException, ValueError, TypeError,
                KeyError, AttributeError, RecursionError, OverflowError):
            # No exception/body/headers/URL is eligible for logs.
            print("lookup_stopped_inconclusive")
