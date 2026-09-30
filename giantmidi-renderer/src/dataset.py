from __future__ import annotations

import datetime
import http.cookiejar
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

from .config import config

logger = logging.getLogger(__name__)


def get_midi_duration(path: Path) -> Optional[float]:
    """Return MIDI duration in seconds using mido, or None if invalid/corrupt."""
    try:
        import mido
        mid = mido.MidiFile(str(path))
        length = float(mid.length)
        if math.isnan(length) or math.isinf(length) or length <= 0.05:
            logger.warning(f"Unusable MIDI duration ({length}) for {path.name}")
            return None
        return length
    except Exception as e:
        logger.warning(f"Failed to read MIDI duration with mido for {path.name}: {e}")
        return None


def resolve_drive_folder_file_id(folder_id: str, target_filename: str = "midis_v1.2.zip") -> str:
    """
    Attempt to resolve file ID dynamically from public Google Drive folder page.
    Falls back to verified constant if dynamic parse fails.
    """
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            pos = html.find(target_filename)
            if pos != -1:
                chunk = html[max(0, pos - 300) : min(len(html), pos + 300)]
                match = re.search(r'5:auSv138:([a-zA-Z0-9_-]{28,35})-0', chunk)
                if match:
                    resolved_id = match.group(1)
                    logger.info(f"Dynamically resolved {target_filename} file ID: {resolved_id}")
                    return resolved_id
    except Exception as e:
        logger.warning(f"Could not resolve folder dynamically ({e}). Using verified fallback ID.")
    return config.giantmidi_zip_file_id


def download_google_drive_file(
    file_id: str,
    dest_path: Path,
    label: str = "archive",
    max_retries: int = 3,
) -> None:
    """
    Download large file from Google Drive handling virus scan warning interstitial.
    Includes exponential backoff retries and archive structure validation.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Google Drive for {label} (attempt {attempt}/{max_retries}, ID: {file_id})...")
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

            init_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            req = urllib.request.Request(
                init_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )

            with opener.open(req, timeout=30) as resp:
                peek = resp.read(1024)

                # Check if it is a direct binary zip stream
                if peek.startswith(b"PK\x03\x04"):
                    logger.info(f"Direct binary zip stream detected for {label}...")
                    with open(temp_path, "wb") as out_file:
                        out_file.write(peek)
                        chunk_size = 1024 * 1024
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            out_file.write(chunk)
                else:
                    html = (peek + resp.read()).decode("utf-8", errors="ignore")

                    # Check for quota exceeded error page
                    if "download quota" in html.lower() or "too many users" in html.lower():
                        raise RuntimeError(
                            "Google Drive download quota exceeded for this public resource. "
                            "Please retry later or use actions/cache."
                        )

                    # Extract form inputs from warning page
                    inputs = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html))
                    action_match = re.search(r'<form id="download-form" action="([^"]+)"', html)
                    action_url = action_match.group(1) if action_match else "https://drive.usercontent.google.com/download"

                    if not inputs:
                        confirm_match = re.search(r'confirm=([0-9A-Za-z_]+)', html)
                        confirm_val = confirm_match.group(1) if confirm_match else "t"
                        inputs = {"id": file_id, "export": "download", "confirm": confirm_val}

                    query_str = urllib.parse.urlencode(inputs)
                    download_url = f"{action_url}?{query_str}"
                    logger.info(f"Downloading from confirmed URL: {action_url}...")

                    dl_req = urllib.request.Request(
                        download_url,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    )

                    chunk_size = 1024 * 1024
                    with opener.open(dl_req, timeout=60) as dl_resp, open(temp_path, "wb") as out_file:
                        total_size = int(dl_resp.headers.get("Content-Length", 0))
                        pbar = None
                        if tqdm and total_size > 0:
                            pbar = tqdm(total=total_size, unit="B", unit_scale=True, desc=f"Downloading {label}")

                        while True:
                            chunk = dl_resp.read(chunk_size)
                            if not chunk:
                                break
                            out_file.write(chunk)
                            if pbar:
                                pbar.update(len(chunk))
                        if pbar:
                            pbar.close()

            downloaded_size = temp_path.stat().st_size if temp_path.exists() else 0
            if downloaded_size < 50 * 1024 * 1024:
                raise ValueError(
                    f"Downloaded file suspiciously small ({downloaded_size} bytes). "
                    "Likely an error page or truncated download."
                )

            if not zipfile.is_zipfile(temp_path):
                raise ValueError(f"Downloaded file at {temp_path} is not a valid zip archive.")

            temp_path.rename(dest_path)
            logger.info(f"Successfully verified and saved {label} to {dest_path} ({dest_path.stat().st_size} bytes).")
            return

        except Exception as e:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            logger.warning(f"Download attempt {attempt} failed: {e}")
            if attempt < max_retries:
                backoff = attempt * 10
                logger.info(f"Retrying in {backoff} seconds...")
                time.sleep(backoff)
            else:
                raise


def download_preview_midis(dest_dir: Path) -> List[Path]:
    """Download preview MIDIs directly from GitHub repository for fast smoke testing."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    preview_files = [
        "Chopin, Frédéric, Études, Op.10, g0hoN6_HDVU.mid",
        "Handel, George Frideric, Air in E major, HWV 425, bNzVz5byPqk.mid",
        "Liszt, Franz, Hungarian Rhapsody No.2, S.244_2, LdH1hSWGFGU.mid",
        "Ravel, Maurice, Jeux d'eau, v-QmwrhO3ec.mid",
    ]
    downloaded = []
    base_url = config.dataset_preview_url
    for fname in preview_files:
        out_file = dest_dir / fname
        if not out_file.exists():
            encoded = urllib.parse.quote(fname)
            url = f"{base_url}/{encoded}"
            try:
                logger.info(f"Downloading preview MIDI: {fname}...")
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp, open(out_file, "wb") as f:
                    f.write(resp.read())
            except Exception as e:
                logger.warning(f"Failed to fetch preview {fname}: {e}")
                continue
        if out_file.exists():
            downloaded.append(out_file)
    return downloaded


def ensure_dataset(smoke_test: bool = False, force_download: bool = False) -> Path:
    """
    Ensure the GiantMIDI-Piano dataset is available in data/midis/.
    In non-smoke mode, fails closed on download error rather than silently degrading.
    """
    midi_dir = config.midi_dir
    midi_dir.mkdir(parents=True, exist_ok=True)

    if smoke_test:
        logger.info("Smoke test mode: downloading preview MIDIs exclusively (bypassing Google Drive)...")
        downloaded = download_preview_midis(midi_dir)
        if not downloaded:
            raise RuntimeError("Failed to download preview MIDIs for smoke test.")
        return midi_dir

    # Check if we already have .mid files
    existing_midis = [
        p for p in midi_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".mid", ".midi"}
    ]
    if existing_midis and not force_download:
        logger.info(f"Dataset already present: {len(existing_midis)} MIDI files in {midi_dir}")
        return midi_dir

    zip_path = config.data_dir / config.giantmidi_zip_name
    if not zip_path.exists() or force_download:
        file_id = resolve_drive_folder_file_id(config.giantmidi_drive_folder_id)
        # Fail closed on dataset download error in production runs
        download_google_drive_file(
            file_id,
            zip_path,
            label="GiantMIDI-Piano dataset archive",
        )

    # Extract ZIP
    logger.info(f"Extracting {zip_path.name} into {midi_dir}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            fname = Path(member.filename).name
            if fname.lower().endswith((".mid", ".midi")):
                target = midi_dir / fname
                if not target.exists():
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())

    extracted = [p for p in midi_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mid", ".midi"}]
    if not extracted:
        raise RuntimeError(f"No MIDI files found after extracting {zip_path.name}.")
    logger.info(f"Extraction complete. Found {len(extracted)} files in {midi_dir}.")
    return midi_dir


def load_processed_set(log_path: Path) -> Set[str]:
    """Read the processed.txt ledger and return set of completed MIDI stems/filenames."""
    processed = set()
    if not log_path.exists():
        return processed

    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            fname = parts[0].strip()
            processed.add(fname)
            processed.add(Path(fname).stem)
    return processed


def append_processed_entry(
    log_path: Path,
    midi_filename: str,
    duration_s: float,
    frames: int,
    file_size_bytes: int,
    storage_url: str,
) -> None:
    """Record a successfully rendered MIDI into processed.txt."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = f"{midi_filename}\t{duration_s:.2f}s\t{frames}f\t{file_size_bytes}B\t{timestamp}\t{storage_url}\n"

    if not log_path.exists() or log_path.stat().st_size == 0:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("# GiantMIDI-Piano Processed Ledger\n")
            f.write("# filename\tduration\tframes\tsize\ttimestamp\tstorage_url\n")

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    logger.info(f"Recorded success for {midi_filename} in {log_path.name}")


def load_failed_map(failed_log: Path) -> Tuple[Set[str], Dict[str, int]]:
    """
    Read failed_pieces.txt and return:
    1. Set of permanently quarantined pieces (>= 3 attempts or marked PERMANENT).
    2. Dictionary mapping filename to attempt count for retrying transient failures.
    """
    quarantined = set()
    attempts_map: Dict[str, int] = {}

    if not failed_log.exists():
        return quarantined, attempts_map

    with open(failed_log, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            fname = parts[0].strip()
            attempts = 1
            is_perm = False
            if len(parts) >= 3:
                try:
                    attempts = int(parts[2].strip())
                except ValueError:
                    attempts = 1
            if len(parts) >= 4 and "PERMANENT" in parts[3].upper():
                is_perm = True

            attempts_map[fname] = max(attempts_map.get(fname, 0), attempts)
            attempts_map[Path(fname).stem] = max(attempts_map.get(Path(fname).stem, 0), attempts)

            if is_perm or attempts >= 3:
                quarantined.add(fname)
                quarantined.add(Path(fname).stem)

    return quarantined, attempts_map


def append_failed_entry(
    failed_log: Path,
    midi_filename: str,
    error_message: str,
    is_permanent: bool = False,
    attempts: int = 1,
) -> None:
    """Record an error entry in failed_pieces.txt with retry tracking."""
    failed_log.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    clean_error = error_message.replace("\t", " ").replace("\n", " ")[:200]
    category = "PERMANENT" if is_permanent else "TRANSIENT"
    entry = f"{midi_filename}\t{timestamp}\t{attempts}\t{category}\t{clean_error}\n"

    if not failed_log.exists() or failed_log.stat().st_size == 0:
        with open(failed_log, "w", encoding="utf-8") as f:
            f.write("# GiantMIDI-Piano Failure Quarantine Ledger\n")
            f.write("# filename\ttimestamp\tattempts\tcategory\terror_message\n")

    with open(failed_log, "a", encoding="utf-8") as f:
        f.write(entry)
    logger.warning(f"Recorded {category} failure for {midi_filename} (attempt {attempts}) in {failed_log.name}")


def pick_next_midi(
    midi_dir: Path,
    processed_set: Set[str],
    quarantined_set: Optional[Set[str]] = None,
    max_duration_seconds: Optional[float] = None,
    specific_piece: Optional[str] = None,
    allow_exceed_duration: bool = False,
) -> Optional[Tuple[Path, float]]:
    """Find the next eligible MIDI file matching criteria."""
    max_dur = max_duration_seconds or config.max_piece_duration_seconds
    quarantined_set = quarantined_set or set()

    all_files = sorted(
        p for p in midi_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".mid", ".midi"}
    )

    if specific_piece:
        for p in all_files:
            if specific_piece.lower() in p.name.lower():
                dur = get_midi_duration(p)
                if dur is None:
                    logger.error(f"Specific piece '{p.name}' is unparseable or corrupt.")
                    return None
                if p.name in processed_set or p.stem in processed_set:
                    logger.warning(f"Specific piece '{p.name}' was already processed. Re-rendering per request.")
                if p.name in quarantined_set or p.stem in quarantined_set:
                    logger.warning(f"Specific piece '{p.name}' was quarantined. Retrying per explicit request.")
                if dur > max_dur and not allow_exceed_duration:
                    logger.error(
                        f"Specific piece '{p.name}' duration ({dur:.1f}s) exceeds max duration ({max_dur:.1f}s). "
                        "Refusing to run to protect CI time budget."
                    )
                    return None
                return p, dur
        logger.warning(f"Specific piece '{specific_piece}' not found in {midi_dir}")
        return None

    # Scan files for the next eligible candidate
    skipped_long = 0
    skipped_corrupt = 0
    for p in all_files:
        if p.name in processed_set or p.stem in processed_set:
            continue
        if p.name in quarantined_set or p.stem in quarantined_set:
            continue

        dur = get_midi_duration(p)
        if dur is None:
            # Corrupt MIDI — quarantine permanently immediately
            append_failed_entry(config.failed_log, p.name, "Unparseable MIDI format / mido parse error", is_permanent=True)
            quarantined_set.add(p.name)
            quarantined_set.add(p.stem)
            skipped_corrupt += 1
            continue

        if dur <= max_dur:
            logger.info(f"Selected candidate: '{p.name}' (duration: {dur:.1f}s, limit: {max_dur:.1f}s)")
            return p, dur
        else:
            skipped_long += 1
            logger.debug(f"Skipping {p.name} — duration {dur:.1f}s exceeds limit {max_dur:.1f}s")

    if skipped_long or skipped_corrupt:
        logger.info(f"Scan summary: {skipped_long} exceeded duration limit ({max_dur}s), {skipped_corrupt} were corrupt.")
    logger.warning(f"No eligible unprocessed MIDI found in {midi_dir} within duration limit {max_dur}s")
    return None
