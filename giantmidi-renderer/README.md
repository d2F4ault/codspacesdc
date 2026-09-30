# GiantMIDI-Piano → Silent MP4 → Cloud Storage

A fully automated GitHub Actions pipeline that:

1. **Fetches** the next unprocessed MIDI from the [GiantMIDI-Piano](https://github.com/bytedance/GiantMIDI-Piano) dataset
2. **Renders** it into a frame-accurate silent MP4 using headless Chromium + Playwright (CPU-only, no GPU required)
3. **Uploads** the video to free S3-compatible cloud storage (Cloudflare R2 or Backblaze B2)
4. **Records** the piece in a commit-backed state ledger so nothing ever renders twice

Zero manual steps after initial secret configuration. Triggers on a schedule or manually.

---

## How It Works

### Rendering Technique

This pipeline preserves the exact frame-stepping approach from the reference implementation:

- **Headless Chromium** (Playwright) navigates to [app.midiano.com](https://app.midiano.com), uploads the target `.mid` through the page's native file input, and waits for MIDIano to report the song is loaded.
- A **deterministic virtual clock** is injected via `src/deterministic_clock.js` before any page is opened. It overrides `requestAnimationFrame`, `performance.now`, `Date.now`, and patches `AudioContext.currentTime` (which is exactly what MIDIano's `Player.js` reads). Time inside the page only advances when Python explicitly calls `__advanceFrame(dt)` — by exactly `1/60` seconds per frame.
- **Each frame** is captured by compositing all visible `<canvas>` elements onto an offscreen canvas and reading a PNG (`canvas_composite` mode). PNGs stream directly to FFmpeg via subprocess pipe — no intermediate frame files on disk.
- The pipeline captures exactly `round(duration_s × 60)` frames, stepping the virtual clock after each. Output duration and per-note timing are **exact by construction**, regardless of how long each frame takes to capture.
- **CPU-only is fine**: correctness never depended on render speed. A slower machine takes longer wall-clock time but the video is still frame-accurate.
- `ALIGN_VIDEO_TO_MIDI_T0 = True`: MIDIano has a fixed ~2.5s lead-in before the first note (`startDelay = -2.5`). The pipeline skips this pre-roll uncaptured so video `t=0` is the first note strike.

### Why CPU-Only is Sufficient

The original notebook was GPU-specific because it used NVENC hardware encoding and relied on Vulkan/ANGLE for page rendering. This pipeline:
- Uses **`libx264`** (CPU encoder), which is universally available on `ubuntu-latest` runners
- Uses **standard headless Chromium** with software Canvas2D rendering — MIDIano's piano roll is pure Canvas2D, not WebGL, so software rendering is correct
- Captures ~2–4 frames/second on a CPU runner, which is slow but exact; a 5-minute piece takes ~45–75 minutes

### Time Budget & Watchdog Protection

| Step | Time |
|------|------|
| Install FFmpeg + Playwright | ~2–3 min |
| Download/cache dataset | ~3–5 min (first run), ~0 min (cached) |
| Render 5-minute piece | ~45–75 min |
| Upload to R2/B2 | < 2 min |
| **Total** | **< 90 min for a 5-minute piece** |

- The default `MAX_PIECE_DURATION_SECONDS = 300` (5 minutes) keeps jobs safely within budget.
- The frame-capture loop enforces an active watchdog timer (`MAX_JOB_SECONDS`, default 18,000s / 5 hours) and the GitHub workflow sets `timeout-minutes: 350`, guaranteeing jobs will never hit GitHub's 6-hour unhandled hard kill.

### Storage Math & Automatic Quota Pruning

| Setting | Value |
|---------|-------|
| Resolution | 1280 × 720 @ 60 fps |
| Encoder | libx264 `-crf 23 -preset veryfast` |
| Typical bitrate (piano roll) | ~1.5–3 Mbps |
| 5-minute video | ~60–135 MB |
| **Free tier headroom** | **~70–140 videos before pruning** |

When total bucket usage exceeds `MAX_STORAGE_GB` (default 8.5 GB), the oldest videos are automatically deleted down to `PRUNE_TARGET_GB` (default 7.5 GB), reserving headroom for the incoming file. This rolling buffer guarantees the bucket stays safely within free cloud storage limits indefinitely.

---

## Setup

### 1. Fork / Push this Repo

```bash
git init
git add .
git commit -m "feat: initial GiantMIDI renderer pipeline"
git remote add origin https://github.com/YOUR_USERNAME/giantmidi-renderer.git
git push -u origin main
```

### 2. Choose Cloud Storage

#### Option A — Cloudflare R2 (Recommended: 10 GB free forever, zero egress fees)

1. Sign up at [dash.cloudflare.com](https://dash.cloudflare.com) (free)
2. Go to **R2 → Create Bucket** → give it a name (e.g. `giantmidi-videos`)
3. Go to **R2 → Manage R2 API Tokens → Create API Token**
   - Permissions: **Object Read & Write** on your specific bucket
   - Copy the **Access Key ID** and **Secret Access Key**
4. Find your **Account ID** in the right sidebar of any R2 page
5. Your endpoint URL is: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

Set these **GitHub Repository Secrets** (`Settings → Secrets and variables → Actions → New repository secret`):

| Secret Name | Value |
|-------------|-------|
| `R2_ENDPOINT_URL` | `https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | Your R2 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | Your R2 Secret Access Key |
| `R2_BUCKET_NAME` | Your bucket name (e.g. `giantmidi-videos`) |

#### Option B — Backblaze B2 (10 GB free forever, 1 GB/day egress free)

1. Sign up at [backblaze.com/b2](https://www.backblaze.com/b2/cloud-storage.html) (free)
2. Go to **Buckets → Create a Bucket** (make it **Private**)
3. Go to **Application Keys → Add a New Application Key**
   - Allow access to your specific bucket
   - Copy the **keyID** and **applicationKey**
4. Find your **S3-compatible endpoint** under the bucket's **Endpoint** tab (e.g. `https://s3.us-west-004.backblazeb2.com`)

Set these **GitHub Repository Secrets**:

| Secret Name | Value |
|-------------|-------|
| `B2_ENDPOINT_URL` | `https://s3.us-west-004.backblazeb2.com` |
| `B2_KEY_ID` | Your B2 keyID |
| `B2_APPLICATION_KEY` | Your B2 applicationKey |
| `B2_BUCKET_NAME` | Your bucket name |

> [!NOTE]
> The pipeline auto-detects `B2_*`, `R2_*`, or standard `S3_*` secret names. All three naming conventions are supported.

### 3. (Optional) Configure Storage Limits

In `Settings → Secrets and variables → Actions → Variables`, you can set:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_STORAGE_GB` | `8.5` | Prune trigger threshold in GB |
| `MAX_PIECE_DURATION_SECONDS` | `300` | Max piece length in seconds (longer pieces are skipped) |

---

## Running the Pipeline

### Automatic Schedule

The pipeline runs automatically every 4 hours (`cron: '0 */4 * * *'`). To change the frequency, edit `.github/workflows/render.yml`.

### Manual Trigger

1. Go to **Actions → Render GiantMIDI Video → Run workflow**
2. Optional parameters:
   - **max_duration**: Maximum piece duration in seconds (e.g. `120`)
   - **test_seconds**: Render only the first N seconds (e.g. `15` for a quick test)
   - **piece_name**: Target a specific piece by name substring (e.g. `Chopin`)
   - **force_download**: Re-download the dataset archive

### Quick Smoke Test

To verify the full stack (Playwright + Chromium + FFmpeg) without downloading the 193MB dataset or uploading to cloud:

Go to **Actions → Smoke Test → Run workflow**

This uses `--smoke-test` to download only a ~350KB preview MIDI, renders 5 seconds, and validates that Chromium, the deterministic virtual clock, and FFmpeg run without errors. Should complete in ~2 minutes.

---

## State & Failure Tracking

The repository maintains two commit-backed ledgers:

1. **`processed.txt`**: Records successful renders (filename, duration, frame count, size, timestamp, storage URL).
2. **`failed_pieces.txt`**: Quarantines unrenderable or corrupt pieces so that a single broken file never deadlocks future scheduled runs.

- Workflows trigger on `schedule` and `workflow_dispatch` only — **never on `push`** — avoiding commit-retrigger loops.
- The concurrency group `render-pipeline` prevents parallel runs from clashing over the ledgers.
- On any render failure, a debug screenshot is captured and uploaded to GitHub Actions artifacts (`debug-screenshots`) for inspection.

---

## Project Structure

```
giantmidi-renderer/
├── .github/
│   └── workflows/
│       ├── render.yml              # Scheduled + manual render workflow
│       └── smoke-test.yml          # Quick CI smoke test (preview MIDI, no upload)
├── src/
│   ├── __init__.py
│   ├── config.py                   # Configuration, env-var overrides, validation
│   ├── dataset.py                  # Dataset download, MIDI selection, ledgers I/O
│   ├── deterministic_clock.js      # Virtual clock injected into the page
│   ├── renderer.py                 # Headless Playwright + FFmpeg CPU pipe
│   ├── storage.py                  # S3/R2/B2 uploader with quota pruning
│   └── pipeline.py                 # Main CLI orchestrator
├── data/
│   └── .gitkeep                    # Directory tracked; contents gitignored
├── processed.txt                   # Commit-backed success ledger
├── failed_pieces.txt               # Commit-backed failure quarantine ledger
├── requirements.txt
├── .gitignore
├── LICENSE
└── README.md
```

---

## Dataset Attribution

MIDI files sourced from the **GiantMIDI-Piano** dataset under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/):

> Kong, Q., Li, B., Chen, J., & Wang, Y. (2020). *GiantMIDI-Piano: A large-scale MIDI dataset for classical piano music.* arXiv:2010.07061.
> [https://github.com/bytedance/GiantMIDI-Piano](https://github.com/bytedance/GiantMIDI-Piano)

---

## Environment Variable Reference

All settings can be overridden via environment variables for local testing:

| Variable | Default | Description |
|----------|---------|-------------|
| `S3_ENDPOINT_URL` / `R2_ENDPOINT_URL` / `B2_ENDPOINT_URL` | — | S3-compatible endpoint URL |
| `S3_ACCESS_KEY_ID` / `R2_ACCESS_KEY_ID` / `B2_KEY_ID` | — | Access key |
| `S3_SECRET_ACCESS_KEY` / `R2_SECRET_ACCESS_KEY` / `B2_APPLICATION_KEY` | — | Secret key |
| `S3_BUCKET_NAME` / `R2_BUCKET_NAME` / `B2_BUCKET_NAME` | — | Bucket name |
| `S3_REGION_NAME` | `auto` | Region (use `auto` for R2) |
| `MAX_STORAGE_GB` | `8.5` | Prune trigger in GB |
| `PRUNE_TARGET_GB` | `7.5` | Prune target in GB |
| `MAX_PIECE_DURATION_SECONDS` | `300` | Max MIDI length to render |
| `MAX_JOB_SECONDS` | `18000` | Watchdog limit in seconds |
| `RENDER_FPS` | `60` | Output frame rate |
| `RENDER_WIDTH` | `1280` | Video width in pixels |
| `RENDER_HEIGHT` | `720` | Video height in pixels |
| `X264_CRF` | `23` | Quality (lower = better, larger file) |
| `X264_PRESET` | `veryfast` | Encoding speed/compression trade-off |
| `CAPTURE_MODE` | `canvas_composite` | `canvas_composite` or `page_screenshot` |
| `STORAGE_PREFIX` | `giantmidi` | Object key prefix in bucket |
| `MIDI_DIR` | `data/midis` | Override MIDI source directory |
| `OUTPUT_DIR` | `data/output` | Override local video output directory |
| `PROCESSED_LOG` | `processed.txt` | Override success ledger path |
| `FAILED_LOG` | `failed_pieces.txt` | Override failure ledger path |

### Local Testing Without Cloud

If no S3 credentials are set, the pipeline falls back to saving MP4s in `data/output/` locally:

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m src.pipeline --smoke-test --test-seconds 5 --skip-upload
```
