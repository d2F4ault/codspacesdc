from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # ── Workspace & Directories ──────────────────────────────────────────
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    data_dir: Path = field(init=False)
    midi_dir: Path = field(init=False)
    output_dir: Path = field(init=False)
    processed_log: Path = field(init=False)
    failed_log: Path = field(init=False)

    # ── Dataset Settings ────────────────────────────────────────────────
    giantmidi_drive_folder_id: str = "1Stz3CAvMoplo79LR5I3onMWRelCugBYS"
    # 1BDEPaEWFEB2ADquS1VYp5iLZYVngw799 is the verified file ID for midis_v1.2.zip (~184 MB) inside the official folder
    giantmidi_zip_file_id: str = "1BDEPaEWFEB2ADquS1VYp5iLZYVngw799"
    giantmidi_zip_name: str = "midis_v1.2.zip"
    dataset_preview_url: str = (
        "https://raw.githubusercontent.com/bytedance/GiantMIDI-Piano/master/midis_preview"
    )

    # ── Rendering Settings ──────────────────────────────────────────────
    app_url: str = "https://app.midiano.com"
    fps: int = 60
    viewport_w: int = 1280
    viewport_h: int = 720
    capture_mode: str = "canvas_composite"  # "canvas_composite" or "page_screenshot"
    align_video_to_midi_t0: bool = True
    midiano_start_delay_s: float = 2.5
    log_every_frames: int = 300

    # ── CPU Encoding Settings (libx264) ─────────────────────────────────
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    x264_preset: str = "veryfast"
    x264_crf: int = 23
    x264_pix_fmt: str = "yuv420p"

    # ── Execution Limits ────────────────────────────────────────────────
    max_piece_duration_seconds: float = 300.0  # 5 minutes default cap for CI stability
    max_job_seconds: float = 18000.0  # 5 hours watchdog limit for GH Actions (6h max)

    # ── Cloud Storage (S3 / Cloudflare R2 / Backblaze B2) ───────────────
    storage_provider: str = field(init=False)
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = ""
    s3_region_name: str = "auto"
    storage_prefix: str = "giantmidi"
    max_storage_gb: float = 8.5  # safe ceiling under the 10GB free tier
    prune_target_gb: float = 7.5  # prune down to this usage when max is reached

    def __post_init__(self) -> None:
        self.data_dir = self.base_dir / "data"
        self.midi_dir = self.data_dir / "midis"
        self.output_dir = self.data_dir / "output"
        self.processed_log = self.base_dir / "processed.txt"
        self.failed_log = self.base_dir / "failed_pieces.txt"

        # Apply Environment Overrides
        if os.getenv("MIDI_DIR"):
            self.midi_dir = Path(os.getenv("MIDI_DIR"))
        if os.getenv("OUTPUT_DIR"):
            self.output_dir = Path(os.getenv("OUTPUT_DIR"))
        if os.getenv("PROCESSED_LOG"):
            self.processed_log = Path(os.getenv("PROCESSED_LOG"))
        if os.getenv("FAILED_LOG"):
            self.failed_log = Path(os.getenv("FAILED_LOG"))

        if os.getenv("RENDER_FPS"):
            self.fps = int(os.getenv("RENDER_FPS"))
        if os.getenv("RENDER_WIDTH"):
            self.viewport_w = int(os.getenv("RENDER_WIDTH"))
        if os.getenv("RENDER_HEIGHT"):
            self.viewport_h = int(os.getenv("RENDER_HEIGHT"))
        if os.getenv("CAPTURE_MODE"):
            self.capture_mode = os.getenv("CAPTURE_MODE")

        if os.getenv("FFMPEG_BIN"):
            self.ffmpeg_bin = os.getenv("FFMPEG_BIN")
        if os.getenv("FFPROBE_BIN"):
            self.ffprobe_bin = os.getenv("FFPROBE_BIN")
        if os.getenv("X264_PRESET"):
            self.x264_preset = os.getenv("X264_PRESET")
        if os.getenv("X264_CRF"):
            self.x264_crf = int(os.getenv("X264_CRF"))

        if os.getenv("MAX_PIECE_DURATION_SECONDS"):
            self.max_piece_duration_seconds = float(os.getenv("MAX_PIECE_DURATION_SECONDS"))
        if os.getenv("MAX_JOB_SECONDS"):
            self.max_job_seconds = float(os.getenv("MAX_JOB_SECONDS"))

        # Storage credentials (from GitHub Secrets or environment)
        # Note: B2_ENDPOINT_URL is explicitly supported alongside R2/S3
        self.s3_endpoint_url = (
            os.getenv("S3_ENDPOINT_URL")
            or os.getenv("R2_ENDPOINT_URL")
            or os.getenv("B2_ENDPOINT_URL")
            or ""
        )
        self.s3_access_key_id = (
            os.getenv("S3_ACCESS_KEY_ID")
            or os.getenv("R2_ACCESS_KEY_ID")
            or os.getenv("B2_KEY_ID")
            or ""
        )
        self.s3_secret_access_key = (
            os.getenv("S3_SECRET_ACCESS_KEY")
            or os.getenv("R2_SECRET_ACCESS_KEY")
            or os.getenv("B2_APPLICATION_KEY")
            or ""
        )
        self.s3_bucket_name = (
            os.getenv("S3_BUCKET_NAME")
            or os.getenv("R2_BUCKET_NAME")
            or os.getenv("B2_BUCKET_NAME")
            or ""
        )
        self.s3_region_name = os.getenv("S3_REGION_NAME", "auto")

        # Auto-detect Backblaze B2 region from endpoint URL if user left region as auto/default
        if "backblazeb2.com" in self.s3_endpoint_url and (
            not os.getenv("S3_REGION_NAME") or self.s3_region_name == "auto"
        ):
            b2_region_match = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", self.s3_endpoint_url)
            if b2_region_match:
                self.s3_region_name = b2_region_match.group(1)

        self.storage_prefix = os.getenv("STORAGE_PREFIX", "giantmidi").strip("/")

        if os.getenv("MAX_STORAGE_GB"):
            self.max_storage_gb = float(os.getenv("MAX_STORAGE_GB"))
        if os.getenv("PRUNE_TARGET_GB"):
            self.prune_target_gb = float(os.getenv("PRUNE_TARGET_GB"))

        # Check credentials configuration
        has_any_s3 = any([
            self.s3_endpoint_url,
            self.s3_access_key_id,
            self.s3_secret_access_key,
            self.s3_bucket_name,
        ])
        has_all_s3 = all([
            self.s3_endpoint_url,
            self.s3_access_key_id,
            self.s3_secret_access_key,
            self.s3_bucket_name,
        ])

        is_ci = os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("CI") == "true"
        allow_local = os.getenv("ALLOW_LOCAL_STORAGE", "").lower() in ("true", "1")

        # Case 1: Partial S3 credentials provided -> always error
        if has_any_s3 and not has_all_s3:
            missing = []
            if not self.s3_endpoint_url:
                missing.append("S3_ENDPOINT_URL/R2_ENDPOINT_URL/B2_ENDPOINT_URL")
            if not self.s3_access_key_id:
                missing.append("S3_ACCESS_KEY_ID/R2_ACCESS_KEY_ID/B2_KEY_ID")
            if not self.s3_secret_access_key:
                missing.append("S3_SECRET_ACCESS_KEY/R2_SECRET_ACCESS_KEY/B2_APPLICATION_KEY")
            if not self.s3_bucket_name:
                missing.append("S3_BUCKET_NAME/R2_BUCKET_NAME/B2_BUCKET_NAME")
            raise ValueError(
                f"Partial S3/R2/B2 credentials detected. Missing: {', '.join(missing)}. "
                "Refusing silent fallback to ephemeral local storage."
            )

        # Case 2: Running in CI without S3 credentials and without explicit local opt-in
        if is_ci and not has_all_s3 and not allow_local:
            raise ValueError(
                "Running in GitHub Actions CI with no cloud storage secrets configured! "
                "Videos rendered to ephemeral runner disk will be lost upon job completion. "
                "Please configure S3/R2/B2 secrets in repository settings (see README.md), "
                "or set ALLOW_LOCAL_STORAGE=true if running an intentional offline dry run."
            )

        if has_all_s3:
            self.storage_provider = "s3"
        else:
            self.storage_provider = "local"

    @property
    def frame_interval_s(self) -> float:
        return 1.0 / float(self.fps)


# Singleton default config instance
config = Config()
