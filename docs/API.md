# Local API and future integration boundary

Python 3.10+ standard library only. Start with `python server.py --open`; optional `--port 8765` and `--data-dir PATH` (or `APP_DATA_DIR`). Default address is `127.0.0.1:8765`. Data and uploaded files stay under the local data directory. The original planning endpoints need no API key and remain simulated. The separate `/api/video/*` endpoints accept an OpenRouter key through a same-origin password form; it stays only in service memory. Legacy configuration fields remain inert and cannot activate a provider.

## Truthful scope

The original six trend rows are synthetic examples, and six demo asset rows contain metadata only. Real Work observations and private uploaded character media are separate. Original planning endpoints remain simulated; “Approved” approves a plan and does not spend credits. The separate creative endpoints make explicit OpenAI/OpenRouter requests and save their actual results. No endpoint publishes a post to TikTok.

Matching is a deterministic tag-overlap heuristic, constrained by ownership, approval, aspect ratio, selected assets, and video source duration without overlapping source ranges. Still images can cover planned time. The returned `minimum_generation_seconds` equals unmatched seconds in this heuristic plan; it is **not a globally optimized lower bound**. Recorded video durations are user-supplied, not probed from media. Upload validation checks allowed MIME type, base64 integrity, and file signatures, not complete decodability. Browser preview support varies by format (particularly MOV).

## Requests and responses

All writes use `Content-Type: application/json`. Responses are plain objects, never an additional `{data: ...}` wrapper. Errors are `{ "error": "meaningful description" }` with HTTP 400, 403, 404, 409, 413, 415, or 429. Files have `nosniff`; no CORS is enabled. Host is restricted to the active localhost/127.0.0.1 port. Browser writes must be same-origin. Nonbrowser requests without an Origin header are supported. This is a personal local app, not an authenticated multiuser service. Do not expose it through a public tunnel or bind it to a public interface.

- `GET` / `HEAD /media/:filename` serves uploaded files with `Accept-Ranges: bytes`. Single prefix, open-ended, and suffix byte ranges return 206 with `Content-Range`; invalid, multiple, or unsatisfiable ranges return 416 and `Content-Range: bytes */SIZE`. HEAD sends headers only. This permits seeking within local video previews.
- `GET /api/health` → `{status:"ok", planning_mode:"simulation", video_generation_configured:false}`.
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

## Real OpenRouter video generation (separate from planning jobs)

`/generate` is the local connection/generation page. Keys are only in `VideoService` memory, never returned in state, logs or job files. All existing Host/Origin/JSON write restrictions also apply here. `data/video-jobs.json` stores safe job metadata, IDs and input hashes, without media URLs or embedded media; generated MP4s are in `data/generated/`.

- `GET /api/video/connection` → `{connected,provider:"OpenRouter",key_storage:"memory"}`; no upstream request.
- `POST /api/video/connection` with `{api_key}` verifies through the fixed OpenRouter `/api/v1/key` endpoint, without generating video. `DELETE` with `{}` disconnects.
- `GET /api/video/models` → `{models:[...]}` with current Seedance metadata; `GET /api/video/jobs` → `{jobs:[...]}` with local records only. Reading records does not advance paid generation.
- `POST /api/video/jobs` accepts `{idempotency_key,model,prompt,mode,duration,resolution,aspect_ratio,generate_audio,consent:true}`. `mode` is `text`, `frames` (requires `first_frame`, optional `last_frame`), or `references` (1–4 `{type:"image_url"|"video_url",url}` entries). Modes cannot be mixed. Models and parameters are checked against a fresh catalog before posting. Local images are validated PNG/JPEG/WebP data URLs up to5MiB each; video references require HTTPS media-file URLs.
- `POST /api/video/jobs/:id/refresh` queries the same remote ID, throttled to once per30seconds; `POST .../download` stores a validated MP4 locally after completion. Neither operation resubmits generation.
- Local job IDs are `video-<32hex>`. Generated output URLs are `/generated/video-<32hex>.mp4`, with the same Range support as local uploaded videos.

Submission is persisted before the potentially billable POST and never automatically retried. Ambiguous failures or interrupted submissions become `submission_uncertain`; repeated idempotency keys return the same job, and changed contents under the same key are rejected. The server does not trust provider-returned polling/download URLs: it constructs fixed OpenRouter routes from validated IDs. Restarting requires reentering the key; previous task records remain.

Cloud generation is manual-only through `.github/workflows/video-generation.yml` using GitHub Secrets. It does not modify the viewer, reference-video history or planning simulation. No automatic publication endpoint exists.

## OpenAI outfit pairs and publishing drafts

`CreativeService` is independent from `VideoService`. `OPENAI_API_KEY` is retained only in memory, and only fixed `https://api.openai.com/v1/models`, `/images/edits`, and `/responses` routes can receive it. Job records are private `data/creative-jobs.json`; image outputs use `data/generated/style-<32hex>-before.png` and `-after.png`. No key or input image bytes/URLs are persisted.

- `GET /api/creative/connection` returns `{connected,provider:"OpenAI",key_storage:"memory"}`; `POST` accepts `{api_key}` and verifies via model listing; `DELETE {}` disconnects.
- `GET /api/creative/models` returns `{image_models:[{id}],text_models:[{id}]}` from the actual account list, excluding retired GPT Image1 and unsuitable text modalities; no automatic model substitution. `GET /api/creative/jobs` returns safe local job metadata and outputs. Reads do not submit paid jobs.
- `POST /api/creative/images` accepts `{idempotency_key,model,reference_image,before_prompt,after_prompt,consent:true}`. Reference is validated local PNG/JPEG/WebP data (max5MiB) or public HTTPS. It makes two image edits at1024x1536, mediumquality; after references both original character and generatedbefore. Pair status is completed, partial, failed or submission_uncertain, preserving completed output.
- `POST /api/creative/drafts` accepts `{idempotency_key,model,description,character,frames:[],trend_context,search_trends,consent:true}`. Up to4 local image frames, US market, English output. No-search uses one Responses call; search uses web_search then a structured-output call. Output has three captions,3–5 hashtags with reasons, category, actualsourceURL/retrievaltime, plus trend_note. Unverified source/hashtag evidence is downgraded to content; input context alone cannot establish a current trend.
- Generated image GET/HEAD uses the same local file and range restrictions as videos. No raw job/config/key file is served.

Every paid step is recorded before transmission; identical request tokens never automatically retry or continue partial/uncertain work. Both provider keys and explicit consent remain independent. `.github/workflows/creative-generation.yml` uses RepositorySecrets for manual cloud operations and refuses paid reruns. The static viewer receives no key or private job record.

## Owned-character reference-video replication

- `GET /api/replication/catalog` returns eligible real owned/approved character images and effective watched Work reference classifications. Candidate order uses first capture, newest first. Unknown labels are not inferred from appearance or names.
- `PATCH /api/replication/assets/:id` accepts `{material_group:"male"|"female"|"mixed"|"unknown",subject_group:"solo"|"group"|"unknown",apply_character:false}`. Applying to all eligible images with the same explicit character ID/name is opt-in.
- `PATCH /api/replication/references/:id` accepts `{material_group,subject_group}` for a user-confirmed local reference override. It preserves public capture data, dates, history, and daily slots. Known labels require an existing watched reference; an unknown override blocks generation. Overrides are private local IDs/labels/timestamps.
- `POST /api/replication/sync {}` reads only the fixed public GitHub candidate JSON, validates it, and atomically preserves/replaces the local snapshot. It sends no key and submits no generation.
- `POST /api/replication/prepare` accepts `{asset_id,reference_id,reference_video_url,reference_video_confirmed,prompt?}`. It returns a 15-minute opaque `plan_id`, stable `content_fingerprint`, `can_submit`, `blockers`, selected metadata, and `required_inputs:["character_image","reference_video"]`. Preparation is nonpaid. Same known male/female group and confirmed solo cast are required. The actual reference must be an HTTPS video file, not a TikTok share page. A narrow official-player CDN MP4 exception requires its known host/path and explicit `mime_type=video_mp4`; no page/cookie bypass is performed.
- `POST /api/replication/jobs` accepts `{plan_id,idempotency_key,model,duration,resolution,aspect_ratio,generate_audio:false,consent:true}` and returns `{job,asset_id,reference_id,plan_id}`. It revalidates classifications and the exact image content before a references-mode submission containing both the private character image and actual action video. Missing/changed inputs, unsupported models/parameters, expired plans, and absent consent block submission. It never falls back to text or first/last frames.

Reference URLs and plan inputs stay in memory; the image is encoded only for the explicit generation request. This entry point does not host local MP4 files or upload character images to the public repository. GitHub Secrets cannot be read back to supply a local connection.
