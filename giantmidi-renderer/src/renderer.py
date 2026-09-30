from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import config

logger = logging.getLogger(__name__)


def log(msg: str) -> None:
    logger.info(msg)


def fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def probe_duration_seconds(path: Path) -> Optional[float]:
    try:
        out = subprocess.run(
            [
                config.ffprobe_bin,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def png_dimensions(data: bytes) -> Tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return struct.unpack(">II", data[16:24])


def start_ffmpeg_pipe(output_path: Path, width: int, height: int, fps: int) -> subprocess.Popen:
    """Start FFmpeg subprocess reading PNG stream from stdin and encoding with libx264 on CPU."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "image2pipe",
        "-framerate",
        str(fps),
        "-c:v",
        "png",
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        config.x264_preset,
        "-crf",
        str(config.x264_crf),
        "-pix_fmt",
        config.x264_pix_fmt,
        "-profile:v",
        "high",
        "-s",
        f"{width}x{height}",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    proc._stderr_buf = []

    def _drain_stderr() -> None:
        try:
            proc._stderr_buf.append(proc.stderr.read() if proc.stderr else b"")
        except Exception:
            pass

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()
    proc._stderr_thread = t
    return proc


def get_ffmpeg_error_text(proc: subprocess.Popen) -> str:
    t = getattr(proc, "_stderr_thread", None)
    if t is not None:
        t.join(timeout=1.0)
    return b"".join(getattr(proc, "_stderr_buf", [])).decode("utf-8", "replace")


class FrameWriter:
    """Queue PNG bytes to FFmpeg stdin on a background thread so frame capture can overlap encode."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        # Set maxsize to 32 (absorbs encoding jitter while keeping memory usage under ~10MB on 2-vCPU runners)
        self.q: queue.Queue = queue.Queue(maxsize=32)
        self.err: Optional[BaseException] = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                if self.proc.stdin:
                    self.proc.stdin.write(item)
        except BaseException as e:
            self.err = e

    def push(self, data: bytes) -> None:
        if self.err:
            raise RuntimeError(f"FFmpeg stdin writer failed: {self.err}")
        self.q.put(data)

    def finish(self) -> None:
        if self.err:
            raise RuntimeError(f"FFmpeg stdin writer failed: {self.err}")
        try:
            self.q.put(None, timeout=10)
        except queue.Full:
            pass
        self.t.join(timeout=30)
        if self.t.is_alive():
            logger.warning("FFmpeg stdin writer thread did not terminate within timeout.")
        if self.err:
            raise RuntimeError(f"FFmpeg stdin writer failed: {self.err}")


def stop_ffmpeg_gracefully(proc: subprocess.Popen, timeout: float = 60.0) -> int:
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log("  FFmpeg did not finish draining in time — terminating")
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log("  FFmpeg still active — killing")
            proc.kill()
            return proc.wait()


async def wait_piano_ready(page, timeout_ms: int = 60_000) -> None:
    await page.wait_for_selector("canvas.pianoCanvas", timeout=timeout_ms)
    await page.wait_for_function(
        """() => {
            const t = document.body.innerText || '';
            const pianos = document.querySelectorAll('canvas.pianoCanvas');
            return pianos.length >= 1 && !/Creating Buffers|Phrasing/i.test(t);
        }""",
        timeout=timeout_ms,
    )
    await page.wait_for_timeout(1500)


async def upload_midi(page, midi_path: Path) -> None:
    buffers = asyncio.Event()

    def on_console(msg) -> None:
        text = msg.text
        if "Buffers loaded" in text or "Setting song" in text:
            buffers.set()

    page.on("console", on_console)
    file_input = page.locator('input[type="file"][accept*=".mid"]').first
    await file_input.wait_for(state="attached", timeout=30_000)
    await file_input.set_input_files(str(midi_path))
    try:
        await asyncio.wait_for(buffers.wait(), timeout=180)
    except asyncio.TimeoutError:
        log("  (no buffer console event detected — waiting on piano canvas)")
    await wait_piano_ready(page)


async def hide_top_menu(page) -> None:
    btn = page.locator('button[aria-label="Minimize/Maximize Menu"]')
    if await btn.count() and await btn.first.is_visible():
        await btn.first.click(timeout=5_000)
    await page.mouse.move(config.viewport_w // 2, config.viewport_h // 2)
    await page.wait_for_timeout(4000)
    await page.evaluate(
        """() => {
            const hide = [
              'button[aria-label="Minimize/Maximize Menu"]',
              'button[aria-label="Open/Close zoom menu"]',
              'button[aria-label="Open settings"]',
              'button[aria-label="Open menu"]',
            ];
            for (const sel of hide) {
              document.querySelectorAll(sel).forEach(el => { el.style.display = 'none'; });
            }
        }"""
    )


async def capture_frame(page) -> bytes:
    if config.capture_mode == "canvas_composite":
        data_url = await page.evaluate("(dt) => window.__captureThenStep(dt)", config.frame_interval_s)
        return base64.b64decode(data_url.split(",", 1)[1])
    png = await page.screenshot(type="png", full_page=False)
    await page.evaluate("(dt) => window.__advanceFrame(dt)", config.frame_interval_s)
    return png


async def render_midi_to_mp4(
    midi_path: Path,
    output_path: Path,
    test_seconds: Optional[float] = None,
) -> Tuple[Path, float, int]:
    """
    Render a single MIDI file into an exact, frame-accurate silent MP4 using
    headless Chromium and deterministic virtual clock frame-stepping.
    """
    from playwright.async_api import async_playwright
    import mido

    # Read duration safely
    try:
        midi_file = mido.MidiFile(str(midi_path))
        duration_s = max(float(midi_file.length), 0.1)
    except Exception as e:
        raise ValueError(f"Failed to read MIDI file {midi_path.name}: {e}")

    if test_seconds is not None:
        duration_s = min(duration_s, float(test_seconds))
        log(f"Test mode active: rendering first {duration_s:.1f}s of {midi_path.name}")

    total_frames = max(1, int(round(duration_s * config.fps)))
    preroll_frames = (
        int(round(float(config.midiano_start_delay_s) * config.fps))
        if config.align_video_to_midi_t0
        else 0
    )

    log(f"Rendering: {midi_path.name}")
    log(
        f"  Duration: {duration_s:.2f}s → {total_frames} frames @ {config.fps} FPS "
        f"({config.viewport_w}x{config.viewport_h}, {config.capture_mode}, "
        f"preroll: {preroll_frames} frames uncaptured)"
    )

    # Use runner temp dir (tens of GB free) — avoid /dev/shm which is capped at 64MB
    temp_dir = Path(tempfile.gettempdir()) / "giantmidi_render"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_mp4 = temp_dir / f"{midi_path.stem}_render.mp4"
    if temp_mp4.exists():
        temp_mp4.unlink()

    clock_js_path = Path(__file__).parent / "deterministic_clock.js"
    clock_js = clock_js_path.read_text(encoding="utf-8")

    launch_args = [
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu-sandbox",
        "--disable-extensions",
        "--mute-audio",
        "--hide-scrollbars",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        f"--window-size={config.viewport_w},{config.viewport_h}",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=launch_args,
        )
        context = await browser.new_context(
            viewport={"width": config.viewport_w, "height": config.viewport_h},
            device_scale_factor=1,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(clock_js)
        await context.grant_permissions(["midi", "midi-sysex"], origin=config.app_url)

        page = await context.new_page()
        ffmpeg = None
        writer = None
        loop = asyncio.get_running_loop()

        try:
            log("  Navigating to MIDIano web application...")
            await page.goto(config.app_url, wait_until="domcontentloaded", timeout=60_000)
            await wait_piano_ready(page)

            installed = await page.evaluate("() => typeof window.__advanceFrame === 'function'")
            if not installed:
                raise RuntimeError("Deterministic-clock init script was not installed on page.")

            log("  Uploading MIDI file...")
            await upload_midi(page, midi_path)

            log("  Hiding UI overlays...")
            await hide_top_menu(page)

            log(f"  Starting FFmpeg CPU pipe (libx264) → {temp_mp4}")
            ffmpeg = start_ffmpeg_pipe(temp_mp4, config.viewport_w, config.viewport_h, config.fps)
            writer = FrameWriter(ffmpeg)

            # Engage deterministic virtual clock mode
            await page.evaluate("() => window.__enterSteppedMode()")
            await page.evaluate("() => window.__drainNativeRAF()")
            await page.keyboard.press("Space")

            # Advance preroll to align video t=0 to first note strike
            if preroll_frames:
                log(f"  Advancing {preroll_frames} preroll frames ({config.midiano_start_delay_s}s) to note strike...")
                await page.evaluate(
                    "(args) => { for (let i = 0; i < args.n; i++) window.__advanceFrame(args.dt); }",
                    {"dt": config.frame_interval_s, "n": preroll_frames},
                )

            t0 = time.time()
            first_hash = None
            hash_check_frame = min(90, total_frames)

            for frame_i in range(1, total_frames + 1):
                # Enforce watchdog limit
                if time.time() - t0 > config.max_job_seconds:
                    raise TimeoutError(
                        f"Watchdog limit ({config.max_job_seconds}s) exceeded at frame {frame_i}/{total_frames}."
                    )

                if ffmpeg.poll() is not None:
                    err = get_ffmpeg_error_text(ffmpeg)
                    raise RuntimeError(f"FFmpeg exited early at frame {frame_i}/{total_frames} ({ffmpeg.returncode}):\n{err}")
                if writer.err:
                    raise RuntimeError(f"FFmpeg writer failed: {writer.err}")

                png_bytes = await capture_frame(page)

                if frame_i == 1:
                    w, h = png_dimensions(png_bytes)
                    log(f"  First frame captured: {w}x{h} ({len(png_bytes)} bytes PNG)")
                    # Verify dimensions match requested viewport (diagnostic preserved from reference notebook)
                    if (w, h) not in ((0, 0), (config.viewport_w, config.viewport_h)):
                        log(
                            f"  WARNING: Captured frame dimensions ({w}x{h}) do not match expected viewport "
                            f"({config.viewport_w}x{config.viewport_h}). Check device_scale_factor."
                        )
                    first_hash = hashlib.sha1(png_bytes).hexdigest()
                elif frame_i == hash_check_frame and first_hash is not None:
                    check_hash = hashlib.sha1(png_bytes).hexdigest()
                    if check_hash == first_hash:
                        raise RuntimeError(
                            f"Frame {hash_check_frame} is byte-identical to frame 1. "
                            "The virtual clock is not advancing the canvas."
                        )
                    log(f"  Sanity check passed: frame 1 vs {hash_check_frame} hashes differ.")

                await loop.run_in_executor(None, writer.push, png_bytes)

                if frame_i % config.log_every_frames == 0 or frame_i == total_frames:
                    elapsed = time.time() - t0
                    fps_actual = frame_i / elapsed if elapsed > 0 else 0.0
                    pct = 100.0 * frame_i / total_frames
                    eta = (total_frames - frame_i) / fps_actual if fps_actual > 0 else float("inf")
                    virt = await page.evaluate("() => window.__virtualAudioSeconds")
                    log(
                        f"  Frame {frame_i}/{total_frames} ({pct:.1f}%) | "
                        f"Elapsed: {fmt_hms(elapsed)} | ETA: {fmt_hms(eta)} | "
                        f"{fps_actual:.1f} capture-fps | virt: {virt:.2f}s"
                    )

            log("  Flushing and finalizing video encoder...")
            # Run writer.finish in executor so it does not block the asyncio event loop
            await loop.run_in_executor(None, writer.finish)
            writer = None

            rc = stop_ffmpeg_gracefully(ffmpeg)
            if rc != 0:
                err = get_ffmpeg_error_text(ffmpeg)
                raise RuntimeError(f"FFmpeg failed with exit code {rc}:\n{err}")

            file_size = temp_mp4.stat().st_size if temp_mp4.exists() else 0
            if file_size < 20_000:
                raise RuntimeError(f"Generated video is suspiciously small ({file_size} bytes).")

            # Validate duration with ffprobe if available
            actual_dur = probe_duration_seconds(temp_mp4)
            expected_dur = total_frames / float(config.fps)
            if actual_dur is not None:
                diff = actual_dur - expected_dur
                log(f"  QA: expected {expected_dur:.3f}s, ffprobe reports {actual_dur:.3f}s (diff {diff:+.3f}s)")
                # Threshold warning preserved from reference notebook
                if abs(diff) > 0.05:
                    log(f"  WARNING: Duration mismatch > 50ms ({diff:+.3f}s) — possible timing discrepancy.")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                output_path.unlink()
            shutil.move(str(temp_mp4), str(output_path))
            log(f"  Render complete → {output_path} ({fmt_hms(time.time() - t0)}, {file_size / (1024*1024):.2f} MB)")
            return output_path, duration_s, total_frames

        except Exception:
            # Capture on-failure debug screenshot if browser page is alive
            try:
                debug_path = config.data_dir / f"debug_{midi_path.stem}.png"
                await page.screenshot(path=str(debug_path))
                log(f"  Captured on-failure debug screenshot → {debug_path}")
            except Exception:
                pass

            if writer is not None:
                try:
                    writer.finish()
                except Exception:
                    pass
            if ffmpeg is not None and ffmpeg.poll() is None:
                try:
                    ffmpeg.kill()
                except Exception:
                    pass
            if temp_mp4.exists():
                temp_mp4.unlink(missing_ok=True)
            raise
        finally:
            try:
                if page and not page.is_closed():
                    await page.close()
            except Exception:
                pass
            try:
                if context:
                    await context.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
