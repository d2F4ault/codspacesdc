from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .config import config
from .dataset import (
    append_failed_entry,
    append_processed_entry,
    ensure_dataset,
    load_failed_map,
    load_processed_set,
    pick_next_midi,
)
from .renderer import render_midi_to_mp4
from .storage import get_storage_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GiantMIDI-Piano automated headless rendering pipeline."
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=None,
        help="Maximum MIDI duration in seconds to consider for rendering (default from config).",
    )
    parser.add_argument(
        "--test-seconds",
        type=float,
        default=None,
        help="Render only the first N seconds of the piece (for smoke testing).",
    )
    parser.add_argument(
        "--piece",
        type=str,
        default=None,
        help="Target specific MIDI filename or substring.",
    )
    parser.add_argument(
        "--allow-exceed-duration",
        action="store_true",
        help="Allow specifically targeted piece to exceed max duration.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip uploading the resulting MP4 to cloud storage.",
    )
    parser.add_argument(
        "--force-dataset-download",
        action="store_true",
        help="Force redownloading the GiantMIDI-Piano dataset archive.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Fast smoke test using preview MIDIs only (bypassing Google Drive).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Select the next candidate MIDI and print info without rendering.",
    )
    args = parser.parse_args()

    # Environment variable fallbacks (safe against shell word-splitting in CI)
    if args.max_duration is None and os.getenv("INPUT_MAX_DURATION"):
        try:
            args.max_duration = float(os.getenv("INPUT_MAX_DURATION", ""))
        except ValueError:
            pass

    if args.test_seconds is None and os.getenv("INPUT_TEST_SECONDS"):
        try:
            args.test_seconds = float(os.getenv("INPUT_TEST_SECONDS", ""))
        except ValueError:
            pass

    if args.piece is None and os.getenv("INPUT_PIECE_NAME"):
        piece_val = os.getenv("INPUT_PIECE_NAME", "").strip()
        if piece_val:
            args.piece = piece_val

    if not args.force_dataset_download and os.getenv("INPUT_FORCE_DOWNLOAD", "").lower() in ("true", "1"):
        args.force_dataset_download = True

    if not args.smoke_test and os.getenv("INPUT_SMOKE_TEST", "").lower() in ("true", "1"):
        args.smoke_test = True

    return args


async def run_pipeline() -> int:
    args = parse_args()

    logger.info("=" * 70)
    logger.info("GIANTMIDI-PIANO HEADLESS CPU RENDERING PIPELINE")
    logger.info("=" * 70)

    # 1. Ensure dataset is available
    logger.info("Step 1: Checking GiantMIDI-Piano dataset availability...")
    try:
        midi_dir = ensure_dataset(
            smoke_test=args.smoke_test,
            force_download=args.force_dataset_download,
        )
    except Exception as e:
        logger.error(f"Dataset acquisition failed: {e}", exc_info=True)
        return 1

    # 2. Check state and failure ledgers
    logger.info(f"Step 2: Reading ledgers from {config.processed_log} and {config.failed_log}...")
    processed_set = load_processed_set(config.processed_log)
    quarantined_set, attempts_map = load_failed_map(config.failed_log)
    logger.info(f"  Processed pieces: {len(processed_set)} | Quarantined pieces: {len(quarantined_set)}")

    # 3. Pick candidate piece
    logger.info("Step 3: Selecting next unprocessed candidate piece...")
    candidate = pick_next_midi(
        midi_dir,
        processed_set=processed_set,
        quarantined_set=quarantined_set,
        max_duration_seconds=args.max_duration,
        specific_piece=args.piece,
        allow_exceed_duration=args.allow_exceed_duration,
    )

    if not candidate:
        if args.smoke_test:
            logger.error("Smoke test failed: No preview candidate could be selected.")
            return 1
        logger.info("No eligible unprocessed pieces found matching criteria. Pipeline complete.")
        return 0

    midi_path, duration_s = candidate
    logger.info(f"  Selected: '{midi_path.name}' (duration: {duration_s:.1f}s)")

    if args.dry_run:
        logger.info("Dry run requested — exiting without rendering.")
        return 0

    # 4. Render to MP4
    logger.info(f"Step 4: Rendering '{midi_path.name}' with headless Chromium + libx264...")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    output_mp4 = config.output_dir / f"{midi_path.stem}.mp4"

    try:
        rendered_path, actual_duration_s, total_frames = await render_midi_to_mp4(
            midi_path=midi_path,
            output_path=output_mp4,
            test_seconds=args.test_seconds,
        )
    except Exception as e:
        logger.error(f"Render failed for {midi_path.name}: {e}", exc_info=True)
        # Classify error: Corrupt MIDI format or freeze-frame vs transient infrastructure
        err_str = str(e)
        is_permanent = any(x in err_str.lower() for x in ["byte-identical", "unparseable", "invalid midi", "not a valid"])
        current_attempts = attempts_map.get(midi_path.name, 0) + 1
        append_failed_entry(
            config.failed_log,
            midi_path.name,
            err_str,
            is_permanent=is_permanent,
            attempts=current_attempts,
        )
        return 1

    file_size_bytes = rendered_path.stat().st_size if rendered_path.exists() else 0
    if not rendered_path.exists() or file_size_bytes < 20_000:
        logger.error(f"Render produced missing or invalid file ({file_size_bytes} bytes).")
        return 1

    logger.info(f"Render completed successfully: {rendered_path.name} ({file_size_bytes / (1024*1024):.2f} MB)")

    # 5. Cloud Storage Upload
    storage_url = "local"
    if not args.skip_upload:
        logger.info(f"Step 5: Uploading {rendered_path.name} to cloud storage...")
        try:
            storage = get_storage_provider()
            storage_url = storage.upload_file(rendered_path)
            logger.info(f"  Cloud destination: {storage_url}")
            # Delete local file after successful cloud upload to save runner disk space
            if rendered_path.exists() and storage_url.startswith("s3://"):
                rendered_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Upload failed: {e}", exc_info=True)
            # Transient upload failure — do not permanently quarantine the MIDI file
            current_attempts = attempts_map.get(midi_path.name, 0) + 1
            append_failed_entry(
                config.failed_log,
                midi_path.name,
                f"Upload error: {e}",
                is_permanent=False,
                attempts=current_attempts,
            )
            return 1
    else:
        logger.info("Step 5: Upload skipped (--skip-upload set).")

    # 6. Record in state ledger
    if args.test_seconds is None and not args.skip_upload:
        # Only record as processed if successfully stored in S3 or if local storage is explicitly allowed
        allow_local = os.getenv("ALLOW_LOCAL_STORAGE", "").lower() in ("true", "1")
        if storage_url.startswith("s3://") or allow_local:
            logger.info(f"Step 6: Recording completion in {config.processed_log}...")
            append_processed_entry(
                config.processed_log,
                midi_filename=midi_path.name,
                duration_s=actual_duration_s,
                frames=total_frames,
                file_size_bytes=file_size_bytes,
                storage_url=storage_url,
            )
        else:
            logger.warning(
                "Step 6: Skipping ledger update because file was only stored on ephemeral runner disk. "
                "Cloud credentials are required to permanently mark pieces as processed."
            )
    else:
        logger.info("Step 6: State ledger update skipped (test mode or upload skipped).")

    logger.info("=" * 70)
    logger.info("PIPELINE RUN COMPLETED SUCCESSFULLY")
    logger.info("=" * 70)
    return 0


def main() -> None:
    code = asyncio.run(run_pipeline())
    sys.exit(code)


if __name__ == "__main__":
    main()
