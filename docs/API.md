# Local API and future integration boundary

Python 3.10+ standard library only. Start with `python server.py --open`; optional `--port 8765` and `--data-dir PATH` (or `APP_DATA_DIR`). Default address is `127.0.0.1:8765`. Data and uploaded files stay under the local data directory. No API key is needed or accepted. Configuration fields are placeholders and cannot activate a provider.

## Truthful scope

All six trend rows are labeled synthetic examples, not TikTok observations or validated popularity rankings. Six demo asset rows contain metadata only; they have no playable video. Uploaded media bytes are stored locally and can be previewed, but this app does not transcode, inspect visual content, render a finished film, call Seedance, or publish. “Approved” means the user approved a simulated plan. The only export is a JSON manifest. `Seedance 2.5` is a requested product label, not a verified API model identifier.

Matching is a deterministic tag-overlap heuristic, constrained by ownership, approval, aspect ratio, selected assets, and video source duration without overlapping source ranges. Still images can cover planned time. The returned `minimum_generation_seconds` equals unmatched seconds in this heuristic plan; it is **not a globally optimized lower bound**. Recorded video durations are user-supplied, not probed from media. Upload validation checks allowed MIME type, base64 integrity, and file signatures, not complete decodability. Browser preview support varies by format (particularly MOV).

## Requests and responses

All writes use `Content-Type: application/json`. Responses are plain objects, never an additional `{data: ...}` wrapper. Errors are `{ "error": "meaningful description" }` with HTTP 400, 403, 404, 409, 413, 415, or 429. Files have `nosniff`; no CORS is enabled. Host is restricted to the active localhost/127.0.0.1 port. Browser writes must be same-origin. Nonbrowser requests without an Origin header are supported. This is a personal local app, not an authenticated multiuser service. Do not expose it through a public tunnel or bind it to a public interface.

- `GET` / `HEAD /media/:filename` serves uploaded files with `Accept-Ranges: bytes`. Single prefix, open-ended, and suffix byte ranges return 206 with `Content-Range`; invalid, multiple, or unsatisfiable ranges return 416 and `Content-Range: bytes */SIZE`. HEAD sends headers only. This permits seeking within local video previews.
- `GET /api/health` → `{status:"ok", mode:"simulation", network_calls:0}`.
- `GET /api/state` → `{trends:[], assets:[], jobs:[], config:{}, mode:"simulation"}`.
- `PUT /api/config` accepts known config keys; unknown keys and enabling a live provider are rejected. `api_base_url` and `generation_model` are inert future settings. URL credentials and query strings are rejected. `max_daily_jobs` (1–100) is enforced on job creation, counting all created jobs for the UTC date. Retries reuse existing jobs.
- `POST /api/assets` accepts `{name, kind, duration, aspect_ratio, tags, rights:"owned", approved, data_url}`. `data_url` must contain PNG/JPEG/WebP/GIF or MP4/WebM/MOV base64. Maximum entire JSON body: 50 MiB (so raw file must be below ~37 MiB). Tags must be nonempty. Successful response 201 is the saved asset, with `/media/asset-ID.ext` URL. Unowned assets are rejected. Approval alone does not certify legal rights.
- `PATCH /api/assets/:id` accepts `name`, `tags`, `duration`, `aspect_ratio`, `approved` only.
- `DELETE /api/assets/:id` with `{}` deletes unreferenced asset and its local media. Referenced assets return 409 to preserve existing plan integrity.
- `POST /api/match` accepts `{trend_id, asset_ids?:[], use_demo?:true}`. Omitted asset IDs uses all eligible assets; an empty array intentionally uses none. Missing IDs return 404. Result: `{trend_id,segments,duration,owned_ratio,generation_seconds,minimum_generation_seconds,eligible_assets,notes,prompt}`. `eligible_assets` is a **number**. Each segment contains `index,label,duration,start,mode,asset_id,match_score`. Owned segments also have `asset_kind,source_in,source_out,is_demo,render_mode`. `render_mode` is `clip` or `still_image`.
- `POST /api/jobs` uses match fields plus optional `title`, `prompt`, `idempotency_key`. Repeating the same nonempty key returns the same job, even if other request fields differ. Clients should mint a new key for each deliberate new job and retain it for transport retries. Jobs persist across restarts.
- `GET /api/jobs/:id` gets a job. Both this route and state polling advance simulation state after elapsed time. `queued` → `simulating` (~0.8 s) → `pending_review` (~4 s). No background network or actual generation runs.
- `POST /api/jobs/:id/review` accepts `{decision:"approved"|"revise"|"rejected",note:""}` only when pending review. Result status is `approved`, `needs_revision`, or `rejected`.
- `POST /api/jobs/:id/retry` with `{}` restarts a needs_revision/rejected/cancelled/failed job simulation. It does not edit the plan or call an API.
- `POST /api/jobs/:id/cancel` with `{}` cancels a queued/simulating job.
- `GET /api/jobs/:id/export` downloads a manifest containing explicit `videos_generated:0`, `network_calls:0`, `published:false`, `mode:"simulation"` and the saved job. `result_url` remains null.

Job example:

```json
{"id":"job-...","title":"晨间仪式感","trend_id":"trend-1","status":"queued","created_at":"...","updated_at":"...","segments":[],"duration":12,"owned_ratio":1,"generation_seconds":0,"prompt":"...","is_simulation":true,"contains_demo_assets":true,"review_note":"","result_url":null,"execution":"simulation_only","gates":["simulation_only","human_review_required","live_generation_not_configured","publishing_disabled","demo_assets_not_real_media"]}
```

## Reserved adapter interfaces

`adapters.py` declares `TrendProvider.discover`, `AssetProvider.list_owned`, and `GenerationProvider.submit/status`. `DisabledGenerationProvider` deliberately raises. These interfaces are not connected to HTTP settings and no secrets are stored. A future integration must separately verify provider/model identity and actual official API capabilities, load credentials server-side, add real error/status handling and durable work execution, obtain scoped upload/cost authorization, and enforce minimum owned ratio and human review before any live action. Trend integration must use an authorized source and record observation timestamps and provenance. Publication needs a distinct authorized adapter; none exists here.

Run tests: `python -m unittest discover -s tests -v`. Tests use temporary directories and ephemeral loopback ports.

## 免费公开视频发现

- `GET /api/discovery`：候选库与采集历史。
- `POST /api/discovery/search`：`{"kind":"hands","query":"","limit":12}`。支持 hands / transition / pov / all，返回 202 和后台任务。query 只筛选两篇公开文章的标题与描述，不执行全站搜索。limit 为 1–30。
- `POST /api/discovery/runs/:id`：读取本地任务状态，不另发采集请求。
- `POST /api/discovery/import`：`{"url":"https://www.tiktok.com/@creator/video/数字"}`，请求官方 oEmbed；仅接受完整 HTTPS TikTok 视频链接。
- `PATCH /api/discovery/videos/:id/favorite`：`{"favorite":true}`。

仅公开来源，不使用 API Key。采集状态 running / succeeded / partial / empty / blocked / failed / interrupted。succeeded 仅代表公开页面读取成功，不代表视频播放、热度或动作通过审核。每条候选保留 motion_verified=false、trend_verified=false；不推断未观测指标。
