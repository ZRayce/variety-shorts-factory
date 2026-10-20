"""Local-first Variety Shorts Factory API and static web application."""

from __future__ import annotations

import hashlib
import json
import math
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import textwrap
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlsplit


APP_ROOT = Path(__file__).resolve().parent


def load_project_env(path: Path) -> None:
    """Load only documented app settings from an optional ignored .env file."""
    allowed = {"VSF_DATA_DIR", "VSF_PORT", "VSF_MAX_UPLOAD_BYTES", "VSF_MAX_SOURCE_SECONDS", "VSF_FFMPEG", "VSF_FFPROBE"}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = (part.strip() for part in entry.split("=", 1))
        if key in allowed and key not in os.environ:
            os.environ[key] = os.path.expandvars(value.strip("\"'"))


load_project_env(APP_ROOT.parent / ".env")
DATA_ROOT = Path(os.environ.get("VSF_DATA_DIR", Path.home() / "Videos" / "VarietyShortsFactory")).expanduser().resolve()
SOURCE_ROOT = DATA_ROOT / "sources"
OUTPUT_ROOT = DATA_ROOT / "outputs"
THUMB_ROOT = DATA_ROOT / "thumbnails"
LOG_ROOT = DATA_ROOT / "logs"
DB_PATH = DATA_ROOT / "factory.sqlite3"
MAX_UPLOAD_BYTES = int(os.environ.get("VSF_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_SOURCE_SECONDS = int(os.environ.get("VSF_MAX_SOURCE_SECONDS", "14400"))
MAX_SHORT_SECONDS = 60
MAX_PENDING_JOBS = 24
SCHEMA_VERSION = 8
RENDER_LOCK = threading.Lock()
JOB_QUEUE_LOCK = threading.Lock()
SOURCE_COMMIT_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
FFMPEG = os.environ.get("VSF_FFMPEG", "").strip() or "ffmpeg"
FFPROBE = os.environ.get("VSF_FFPROBE", "").strip() or "ffprobe"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vsf")

for directory in (SOURCE_ROOT, OUTPUT_ROOT, THUMB_ROOT, LOG_ROOT):
    directory.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    """Yield a short-lived SQLite connection with foreign keys enabled and close it on exit."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def resolve_executable(configured: str) -> str | None:
    """Resolve an explicit FFmpeg tool path or a PATH command without shell expansion."""
    if not isinstance(configured, str) or not configured.strip():
        return None
    path = Path(configured).expanduser()
    if path.is_file():
        return str(path.resolve())
    return shutil.which(configured)


def initialize_database() -> None:
    """Create the versioned local schema without replacing existing data."""
    with connect() as db:
        schema_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if schema_table and DB_PATH.is_file():
            old_version = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if old_version and old_version[0] < SCHEMA_VERSION:
                backup_path = DB_PATH.with_name(f"{DB_PATH.stem}.v{old_version[0]}.backup.sqlite3")
                if not backup_path.exists():
                    backup = sqlite3.connect(backup_path)
                    try:
                        db.backup(backup)
                    finally:
                        backup.close()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
            CREATE TABLE IF NOT EXISTS creators (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, duration REAL,
                width INTEGER, height INTEGER, frame_rate REAL, has_audio INTEGER, creator_id TEXT REFERENCES creators(id),
                status TEXT NOT NULL, permission_status TEXT NOT NULL DEFAULT 'unverified',
                permission_evidence TEXT NOT NULL DEFAULT '', permission_expires_at TEXT,
                imported_at TEXT NOT NULL, thumbnail_path TEXT
            );
            CREATE INDEX IF NOT EXISTS assets_hash_idx ON assets(sha256);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), status TEXT NOT NULL,
                stage TEXT NOT NULL, start_seconds REAL NOT NULL, end_seconds REAL NOT NULL,
                preset TEXT NOT NULL, editorial_notes TEXT NOT NULL DEFAULT '', captions_srt TEXT NOT NULL DEFAULT '', output_path TEXT,
                quality_report TEXT, error_category TEXT, diagnostic TEXT, retry_safe INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), decision TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                stage TEXT NOT NULL, outcome TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "captions_srt" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN captions_srt TEXT NOT NULL DEFAULT ''")
        if "crop_x" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN crop_x REAL NOT NULL DEFAULT 50")
        if "crop_y" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN crop_y REAL NOT NULL DEFAULT 50")
        for name, declaration in {
            "profile": "TEXT NOT NULL DEFAULT 'short_vertical'",
            "rights_version": "INTEGER NOT NULL DEFAULT 0",
            "commercial_use": "INTEGER NOT NULL DEFAULT 0",
            "edit_manifest": "TEXT NOT NULL DEFAULT '{}'",
            "log_path": "TEXT",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "timeline_json": "TEXT NOT NULL DEFAULT '[]'",
            "caption_style_json": "TEXT NOT NULL DEFAULT '{}'",
            "project_id": "TEXT",
            "project_version": "INTEGER",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        asset_columns = {row[1] for row in db.execute("PRAGMA table_info(assets)")}
        for name, declaration in {
            "commercial_permission_status": "TEXT NOT NULL DEFAULT 'unverified'",
            "commercial_permission_evidence": "TEXT NOT NULL DEFAULT ''",
            "commercial_permission_expires_at": "TEXT",
            "source_url": "TEXT NOT NULL DEFAULT ''",
            "license_reference": "TEXT NOT NULL DEFAULT ''",
            "attribution": "TEXT NOT NULL DEFAULT ''",
            "restrictions": "TEXT NOT NULL DEFAULT ''",
            "permission_checked_at": "TEXT",
            "permission_version": "INTEGER NOT NULL DEFAULT 0",
            "frame_rate": "REAL",
        }.items():
            if name not in asset_columns:
                db.execute(f"ALTER TABLE assets ADD COLUMN {name} {declaration}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS permission_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL REFERENCES assets(id),
                version INTEGER NOT NULL, creator_id TEXT, permission_status TEXT NOT NULL,
                permission_evidence TEXT NOT NULL, permission_expires_at TEXT,
                commercial_permission_status TEXT NOT NULL, commercial_permission_evidence TEXT NOT NULL,
                commercial_permission_expires_at TEXT, source_url TEXT NOT NULL, license_reference TEXT NOT NULL,
                attribution TEXT NOT NULL, restrictions TEXT NOT NULL, recorded_at TEXT NOT NULL,
                UNIQUE(asset_id,version)
            );
            CREATE TABLE IF NOT EXISTS youtube_candidates (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'media_required', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edit_projects (
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), title TEXT NOT NULL,
                schema_version INTEGER NOT NULL, plan_json TEXT NOT NULL, current_version INTEGER NOT NULL DEFAULT 1,
                review_status TEXT NOT NULL DEFAULT 'draft', review_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS edit_projects_asset_idx ON edit_projects(asset_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS project_versions (
                project_id TEXT NOT NULL REFERENCES edit_projects(id) ON DELETE CASCADE,
                version INTEGER NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(project_id,version)
            );
            CREATE TABLE IF NOT EXISTS project_reviews (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES edit_projects(id),
                job_id TEXT NOT NULL REFERENCES jobs(id), project_version INTEGER NOT NULL,
                decision TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clip_candidates (
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id),
                start_seconds REAL NOT NULL, end_seconds REAL NOT NULL, score INTEGER NOT NULL,
                signals_json TEXT NOT NULL, reasons_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
                score_factors_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending', title TEXT NOT NULL DEFAULT '',
                hook TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', project_id TEXT REFERENCES edit_projects(id),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS clip_candidates_asset_idx ON clip_candidates(asset_id,created_at DESC);
        """)
        candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(clip_candidates)")}
        if "score_factors_json" not in candidate_columns:
            db.execute("ALTER TABLE clip_candidates ADD COLUMN score_factors_json TEXT NOT NULL DEFAULT '[]'")
        db.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))


initialize_database()


def recover_interrupted_jobs() -> None:
    """Mark only in-progress jobs as interrupted; queued work is resumed by the startup queue pump."""
    with connect() as db:
        interrupted = db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
        for row in interrupted:
            db.execute("""UPDATE jobs SET status='failed',stage='recovery',error_category='interrupted',
                         diagnostic='The application stopped before this job completed.',retry_safe=1,updated_at=? WHERE id=?""",
                       (now_iso(), row["id"]))
            event(db, "job", row["id"], "recovery", "interrupted", "Safe user-initiated retry is available.")


def resume_queued_jobs() -> None:
    """Restart bounded queued jobs after the server has recovered interrupted renders."""
    with connect() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT ?",
                          (MAX_PENDING_JOBS,)).fetchall()
    for row in rows:
        start_render_worker(row["id"])


def now_iso() -> str:
    """Return an offset-aware UTC timestamp used for durable audit ordering."""
    return datetime.now(timezone.utc).isoformat()


def safe_filename(raw: str) -> str:
    """Return a basename safe for Windows and POSIX storage or reject unsupported files."""
    if "/" in raw or "\\" in raw:
        raise ValueError("Filename must not contain a path.")
    name = raw.strip().strip(".")
    if not name or len(name) > 180:
        raise ValueError("Filename is empty or too long.")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Choose an MP4, MOV, WebM, MKV, or AVI file.")
    stem = Path(name).stem
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).strip("._") or "media"
    return stem[:140] + Path(name).suffix


def validate_cut(start: float, end: float, duration: float, max_seconds: float | None = MAX_SHORT_SECONDS) -> tuple[float, float]:
    """Validate an inspected-media cut, applying the selected output profile's optional cap."""
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
               for value in (start, end, duration)):
        raise ValueError("Timestamps must be numbers.")
    if start < 0 or end <= start or end > duration or (max_seconds is not None and end - start > max_seconds):
        limit = f"up to {max_seconds:g} seconds " if max_seconds is not None else ""
        raise ValueError(f"Select a valid segment {limit}within the source duration.")
    return float(start), float(end)


def render_profile(profile: str) -> dict[str, int | None]:
    """Return fixed, reviewed output bounds so user input cannot inject FFmpeg filter syntax."""
    profiles = {
        "short_vertical": {"width": 1080, "height": 1920, "max_seconds": MAX_SHORT_SECONDS},
        "long_horizontal": {"width": 1920, "height": 1080, "max_seconds": None},
    }
    if not isinstance(profile, str) or profile not in profiles:
        raise ValueError("Choose a supported output profile.")
    return profiles[profile].copy()


def build_edit_plan(asset_id: str, asset: dict[str, Any], rights_version: int,
                    payload: dict[str, Any], rights_record_id: int | None = None) -> dict[str, Any]:
    """Build a bounded versioned edit-decision plan with ordered source-relative segments."""
    if not isinstance(asset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", asset_id):
        raise ValueError("The source asset identifier is invalid.")
    duration = asset.get("duration")
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or not math.isfinite(duration) or duration <= 0):
        raise ValueError("Inspect the source before creating an edit project.")
    profile_name = payload.get("profile", "short_vertical")
    profile = render_profile(profile_name)
    preset = payload.get("preset", "clean_vertical")
    if preset not in {"clean_vertical", "punchline", "reaction"}:
        raise ValueError("Choose a supported visual preset.")
    content_preset = payload.get("content_preset", "custom")
    if content_preset not in {"gaming", "podcast", "educational", "cinematic", "creator_commentary", "custom"}:
        raise ValueError("Choose a supported content preset.")
    raw_segments = payload.get("segments")
    if raw_segments is None:
        raw_segments = [{"start_seconds": payload.get("start_seconds"), "end_seconds": payload.get("end_seconds")}]
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 100:
        raise ValueError("An edit project needs between 1 and 100 ordered segments.")
    segments = []
    total_duration = 0.0
    segment_ids = set()
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise ValueError("Every timeline segment must be an object.")
        try:
            start, end = validate_cut(raw.get("start_seconds"), raw.get("end_seconds"), duration, None)
        except ValueError as exc:
            raise ValueError(f"Timeline segment {index + 1}: {exc}") from exc
        segment_id = raw.get("id") or str(uuid.uuid4())
        candidate_id = raw.get("candidate_id")
        if (not isinstance(segment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", segment_id)
                or segment_id in segment_ids):
            raise ValueError("Timeline segment IDs must be unique safe identifiers.")
        if candidate_id is not None and (not isinstance(candidate_id, str)
                or not re.fullmatch(r"[A-Fa-f0-9-]{36}", candidate_id)):
            raise ValueError("Timeline candidate references must be valid candidate IDs.")
        segment_ids.add(segment_id)
        total_duration += end - start
        segments.append({"id": segment_id, "order": index,
                         "start_seconds": start, "end_seconds": end,
                         "candidate_id": candidate_id})
    maximum = profile["max_seconds"]
    if maximum is not None and total_duration > maximum:
        raise ValueError(f"The combined Shorts timeline must be no longer than {maximum} seconds.")
    captions = payload.get("captions_srt", "")
    try:
        caption_preset, preset_style = caption_style_preset(profile_name, payload.get("caption_preset"))
        caption_style = normalize_caption_style(payload.get("caption_style", preset_style))
        transcript_cues = normalize_transcript_cues(duration, payload.get("transcript_cues", []))
        captions = validate_caption_timeline(captions, total_duration, caption_style,
                                             100 if profile_name == "short_vertical" else 2000)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    title = bounded_text(payload.get("title"), "Project title", 1, 120, "Untitled edit")
    crop_x = bounded_percent(payload.get("crop_x"), "Horizontal crop position")
    crop_y = bounded_percent(payload.get("crop_y"), "Vertical crop position")
    commercial_use = payload.get("commercial_use", False)
    if not isinstance(commercial_use, bool):
        raise ValueError("Commercial-use selection must be true or false.")
    return {
        "schema_version": 1,
        "title": title,
        "source": {"asset_id": asset_id, "rights_record_id": rights_record_id,
                   "rights_version": rights_version, "duration": float(duration),
                   "width": asset.get("width"), "height": asset.get("height"),
                   "frame_rate": asset.get("frame_rate"), "has_audio": bool(asset.get("has_audio"))},
        "segments": segments,
        "transcript_cues": transcript_cues,
        "editorial_notes": bounded_text(payload.get("editorial_notes"), "Editorial notes", 0, 2000, ""),
        "hook": bounded_text(payload.get("hook"), "Hook", 0, 240, ""),
        "captions": {"format": "srt", "text": captions, "style": caption_style, "preset": caption_preset,
                     "word_level_available": False},
        "editing": {"profile": profile_name, "content_preset": content_preset,
                    "visual_preset": preset, "crop": {"x": crop_x, "y": crop_y, "mode": "fill"}},
        "audio": {"mode": "preserve", "normalize": False, "fade_in_seconds": 0.0,
                  "fade_out_seconds": 0.0},
        "references": {"b_roll": [], "overlays": []},
        "output_profiles": [profile_name],
        "render": {"video_codec": "libx264", "preset": "veryfast", "crf": 22,
                   "pixel_format": "yuv420p"},
        "commercial_use": commercial_use,
        "review": {"status": "draft", "notes": ""},
    }


def generate_candidate_windows(duration: float, scene_times: list[float],
                               silence_intervals: list[tuple[float, float]],
                               minimum_seconds: float, maximum_seconds: float,
                               padding_before: float, padding_after: float,
                               transcript_cues: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Create review-only clip candidates from measured boundaries using explainable signals."""
    numeric = (duration, minimum_seconds, maximum_seconds, padding_before, padding_after)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
           for value in numeric):
        raise ValueError("Candidate settings must be finite numbers.")
    if (duration <= 0 or minimum_seconds < 1 or maximum_seconds < minimum_seconds
            or maximum_seconds > 600 or padding_before < 0 or padding_before > 60
            or padding_after < 0 or padding_after > 60 or duration < minimum_seconds):
        raise ValueError("Candidate duration or padding settings are outside supported bounds.")
    if not isinstance(scene_times, list) or len(scene_times) > 10000:
        raise ValueError("Scene-change data is invalid or too large.")
    clean_scenes = []
    for value in scene_times:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("Scene-change data contains an invalid timestamp.")
        if 0 <= value <= duration:
            clean_scenes.append(float(value))
    clean_scenes.sort()
    if not isinstance(silence_intervals, list) or len(silence_intervals) > 10000:
        raise ValueError("Silence detection data is invalid or too large.")
    clean_silences = []
    for interval in silence_intervals:
        if (not isinstance(interval, (tuple, list)) or len(interval) != 2
                or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       or not math.isfinite(value) for value in interval)):
            raise ValueError("Silence detection contains an invalid interval.")
        start, end = map(float, interval)
        if 0 <= start < end <= duration:
            clean_silences.append((start, end))
    try:
        clean_cues = normalize_transcript_cues(duration, transcript_cues or [], maximum_count=5000)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    cues = [(item["start_seconds"], item["end_seconds"]) for item in clean_cues]

    if clean_scenes:
        proposals = [(max(0.0, moment - padding_before), moment) for moment in clean_scenes]
        proposals.sort()
    else:
        proposals = []
        step = max(minimum_seconds, maximum_seconds * 0.75)
        start = 0.0
        while start + minimum_seconds <= duration and len(proposals) < 50:
            proposals.append((start, None))
            start += step
    results = []
    last_start = -maximum_seconds
    for proposed_start, scene in proposals:
        start = max(0.0, min(proposed_start, duration - minimum_seconds))
        if start - last_start < minimum_seconds * 0.75:
            continue
        if scene is not None:
            end = min(duration, start + maximum_seconds, max(start + minimum_seconds, scene + padding_after))
        else:
            end = min(duration, start + maximum_seconds)
        if end - start < minimum_seconds:
            continue
        last_start = start
        nearby_silence = [(a, b) for a, b in clean_silences
                          if abs(a - start) <= 1.5 or abs(b - end) <= 1.5]
        overlapping_transcript = any(a < end and b > start for a, b in cues)
        signals = ["scene_change" if scene is not None else "evenly_sampled_window"]
        reasons = [f"Scene change near {scene:.2f}s; candidate starts {padding_before:g}s before it."
                   if scene is not None else "Evenly sampled review window; no scene-change signal was detected."]
        score = 30
        factors = [{"name": "baseline_review_window", "points": 30}]
        if scene is not None:
            signals.append("scene_change")
            score += 20
            factors.append({"name": "scene_change", "points": 20})
        if len(nearby_silence) > 0:
            signals.append("nearby_silence_boundary")
            reasons.append("A detected quiet interval is close to a candidate edge; listen to preserve context.")
            score += 10
            factors.append({"name": "nearby_silence_boundary", "points": 10})
        if overlapping_transcript:
            signals.append("transcript_timing")
            reasons.append("Overlaps timestamps from a supplied transcript; wording was not semantically analyzed.")
            score += 15
            factors.append({"name": "transcript_timing", "points": 15})
        if end - start >= maximum_seconds * 0.65:
            score += 10
            factors.append({"name": "usable_duration", "points": 10})
        warnings = []
        if start <= 0:
            warnings.append("Candidate starts at the source boundary; preceding context is unavailable.")
        if end >= duration:
            warnings.append("Candidate ends at the source boundary; following context is unavailable.")
        if not nearby_silence:
            warnings.append("No detected quiet interval is close to a candidate edge; inspect timing manually.")
        results.append({"start_seconds": round(start, 3), "end_seconds": round(end, 3),
                        "duration": round(end - start, 3), "score": min(score, 100),
                        "score_factors": factors, "signals": signals, "reasons": reasons,
                        "warnings": warnings, "title": "", "hook": "", "notes": ""})
        if len(results) >= 50:
            break
    return results


def normalize_transcript_cues(duration: float, transcript_cues: Any, maximum_count: int = 1000) -> list[dict[str, Any]]:
    """Validate small timestamped transcript cues while preserving text only for local edit context."""
    if not isinstance(transcript_cues, list) or len(transcript_cues) > maximum_count:
        raise ValueError(f"Transcript may contain at most {maximum_count} timed cues.")
    cues = []
    for cue in transcript_cues:
        if not isinstance(cue, dict):
            raise ValueError("Transcript cues must be objects.")
        start, end, text = cue.get("start_seconds"), cue.get("end_seconds"), cue.get("text", "")
        if (not isinstance(start, (int, float)) or isinstance(start, bool) or not math.isfinite(start)
                or not isinstance(end, (int, float)) or isinstance(end, bool) or not math.isfinite(end)
                or not isinstance(text, str) or len(text) > 500):
            raise ValueError("Transcript cue values are invalid.")
        if not 0 <= start < end <= duration or not text.strip():
            raise ValueError("Transcript cue timing and text must fit within the source duration.")
        cues.append({"start_seconds": float(start), "end_seconds": float(end), "text": text.strip()})
    return cues


def parse_youtube_video_url(value: str) -> str:
    """Extract only a YouTube video ID; never fetch or download media from the link."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Enter a valid YouTube video URL.")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        host = (parsed.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch":
                values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
                if len(values) != 1:
                    raise ValueError
                video_id = values[0]
            else:
                match = re.fullmatch(r"/(?:shorts|embed)/([A-Za-z0-9_-]{11})", parsed.path)
                if not match:
                    raise ValueError
                video_id = match.group(1)
        elif host in {"youtu.be", "www.youtu.be"}:
            match = re.fullmatch(r"/([A-Za-z0-9_-]{11})", parsed.path)
            if not match:
                raise ValueError
            video_id = match.group(1)
        else:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError
        return video_id
    except ValueError as exc:
        raise ValueError("Enter a valid HTTPS YouTube video URL (playlists are not supported).") from exc


def classify_permission(status: str, expires_at: str | None) -> bool:
    """Return whether a recorded creator grant is current and usable for local editing."""
    if status != "granted":
        return False
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)
    except ValueError:
        return False


def validate_srt(source: str, clip_duration: float, maximum_cues: int = 100) -> str:
    """Validate bounded UTF-8 SRT captions against the rendered clip's time range."""
    if not isinstance(source, str):
        raise ValueError("SRT captions must be valid text under 128 KiB.")
    try:
        if len(source.encode("utf-8")) > 128 * 1024:
            raise ValueError("SRT captions must be valid text under 128 KiB.")
    except UnicodeEncodeError as exc:
        raise ValueError("SRT captions must contain valid Unicode text.") from exc
    normalized = source.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    blocks = re.split(r"\n\s*\n", normalized)
    if isinstance(maximum_cues, bool) or not isinstance(maximum_cues, int) or not 1 <= maximum_cues <= 2000:
        raise ValueError("Caption cue limit is outside supported bounds.")
    if len(blocks) > maximum_cues:
        raise ValueError(f"A caption file may contain at most {maximum_cues} cues.")
    timestamp = re.compile(r"^([0-9]{2,}):([0-5][0-9]):([0-5][0-9])[,.]([0-9]{3}) --> ([0-9]{2,}):([0-5][0-9]):([0-5][0-9])[,.]([0-9]{3})$")

    def milliseconds(match: re.Match[str], offset: int) -> int:
        hours, minutes, seconds, millis = (int(match.group(offset + index)) for index in range(4))
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis

    for index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3 or lines[0].strip() != str(index) or len(lines[2:]) > 5:
            raise ValueError("Each SRT cue needs a sequential number, timing line, and up to five text lines.")
        match = timestamp.fullmatch(lines[1].strip())
        if not match:
            raise ValueError("SRT timing must use HH:MM:SS,mmm --> HH:MM:SS,mmm format.")
        start_ms, end_ms = milliseconds(match, 1), milliseconds(match, 5)
        if end_ms <= start_ms or end_ms > clip_duration * 1000 + 100:
            raise ValueError("Caption timing must be increasing and remain within the selected clip.")
        if any(len(line) > 240 for line in lines[2:]) or not any(line.strip() for line in lines[2:]):
            raise ValueError("Caption text must be nonempty and each line may contain at most 240 characters.")
    return "\n\n".join(block.strip() for block in blocks)


def validate_caption_timeline(source: str, clip_duration: float, style: dict[str, Any],
                              maximum_cues: int = 100) -> str:
    """Validate caption timing and confirm optional wrapping remains readable and in-range."""
    normalized = validate_srt(source, clip_duration, maximum_cues)
    if normalized and style["wrap"]:
        validate_srt(wrap_srt_lines(normalized, style["max_line_length"]), clip_duration, maximum_cues)
    return normalized


CAPTION_STYLE_DEFAULTS = {"font_name": "Arial", "font_size": 22, "color": "#FFFFFF", "outline": 2,
                          "shadow": 1, "placement": "bottom", "safe_margin": 110,
                          "max_line_length": 42, "wrap": True}
CAPTION_STYLE_PRESETS = {
    "shorts": {**CAPTION_STYLE_DEFAULTS, "font_size": 30, "outline": 3, "shadow": 2,
               "placement": "bottom", "safe_margin": 160, "max_line_length": 24},
    "long_form": {**CAPTION_STYLE_DEFAULTS, "font_size": 22, "outline": 2, "shadow": 1,
                  "placement": "bottom", "safe_margin": 70, "max_line_length": 42},
}


def normalize_caption_style(value: Any) -> dict[str, Any]:
    """Validate a small safe caption-style vocabulary before compiling FFmpeg filter settings."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(CAPTION_STYLE_DEFAULTS):
        raise ValueError("Caption style must be a supported settings object.")
    style = {**CAPTION_STYLE_DEFAULTS, **value}
    if not isinstance(style["font_name"], str) or style["font_name"] not in {"Arial", "Aptos", "Segoe UI", "Calibri", "sans-serif"}:
        raise ValueError("Choose a supported system caption font.")
    if (isinstance(style["font_size"], bool) or not isinstance(style["font_size"], int)
            or not 12 <= style["font_size"] <= 64):
        raise ValueError("Caption font size must be from 12 to 64.")
    if not isinstance(style["color"], str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", style["color"]):
        raise ValueError("Caption color must be a six-digit hex color.")
    style["color"] = style["color"].upper()
    for key, maximum in (("outline", 8), ("shadow", 8), ("safe_margin", 300), ("max_line_length", 60)):
        minimum = 10 if key == "safe_margin" else 10 if key == "max_line_length" else 0
        if isinstance(style[key], bool) or not isinstance(style[key], int) or not minimum <= style[key] <= maximum:
            raise ValueError(f"Caption {key.replace('_', ' ')} is outside supported bounds.")
    if (not isinstance(style["placement"], str) or style["placement"] not in {"top", "center", "bottom"}
            or not isinstance(style["wrap"], bool)):
        raise ValueError("Caption placement or wrapping option is invalid.")
    return style


def caption_style_preset(profile: str, preset: str | None = None) -> tuple[str, dict[str, Any]]:
    """Return a readable distinct caption layout for vertical or widescreen content."""
    if preset is None:
        preset = "shorts" if profile == "short_vertical" else "long_form"
    if preset not in CAPTION_STYLE_PRESETS:
        raise ValueError("Choose the Shorts or long-form caption preset, or edit a custom style.")
    return preset, dict(CAPTION_STYLE_PRESETS[preset])


def wrap_srt_lines(source: str, maximum_line_length: int) -> str:
    """Wrap caption text while retaining cue sequence numbers and original millisecond timing."""
    if isinstance(maximum_line_length, bool) or not isinstance(maximum_line_length, int) or not 10 <= maximum_line_length <= 60:
        raise ValueError("Maximum caption line length must be from 10 to 60 characters.")
    normalized = source.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    blocks = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = block.split("\n")
        wrapped = []
        for line in lines[2:]:
            wrapped.extend(textwrap.wrap(line.strip(), width=maximum_line_length, break_long_words=True,
                                         break_on_hyphens=False) or [""])
        blocks.append("\n".join((lines[0], lines[1], *wrapped)))
    return "\n\n".join(blocks)


def caption_filter_style(style: dict[str, Any]) -> str:
    """Build a libass style string using validated font/color/position values only."""
    style = normalize_caption_style(style)
    red, green, blue = style["color"][1:3], style["color"][3:5], style["color"][5:7]
    color = f"&H00{blue}{green}{red}"
    alignment = {"bottom": 2, "center": 5, "top": 8}[style["placement"]]
    return (f"FontName={style['font_name']},FontSize={style['font_size']},PrimaryColour={color},"
            f"Outline={style['outline']},Shadow={style['shadow']},Alignment={alignment},"
            f"MarginV={style['safe_margin']},MarginL={style['safe_margin']},MarginR={style['safe_margin']}")


def resolve_caption_font(preferred: str, fonts_directory: Path | None = None) -> tuple[str, bool]:
    """Use an installed system font or a readable system fallback without bundling licensed fonts."""
    font_files = {"Arial": "arial.ttf", "Aptos": "aptos.ttf", "Segoe UI": "segoeui.ttf",
                  "Calibri": "calibri.ttf", "sans-serif": ""}
    if preferred == "sans-serif":
        return preferred, False
    if fonts_directory is None:
        if os.name != "nt":
            return preferred, False
        fonts_directory = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if (fonts_directory / font_files[preferred]).is_file():
        return preferred, False
    if (fonts_directory / font_files["Arial"]).is_file():
        return "Arial", True
    return "sans-serif", True


def event(db: sqlite3.Connection, entity_type: str, entity_id: str, stage: str, outcome: str, details: str = "") -> None:
    """Append a bounded diagnostic event in the same transaction as its state change."""
    db.execute("INSERT INTO events(entity_type,entity_id,stage,outcome,details,created_at) VALUES(?,?,?,?,?,?)",
               (entity_type, entity_id, stage, outcome, details[:1000], now_iso()))


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a SQLite row to a JSON-compatible mapping."""
    return dict(row)


def remove_generated_file(path: Path, kind: str) -> None:
    """Remove an app-generated temporary file without masking the original operation error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("cleanup failed file_kind=%s", kind)


def read_log_tail(path: Path, limit: int = 1000) -> str:
    """Read only the bounded end of an app-owned render log for concise failure diagnostics."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit), os.SEEK_SET)
            return stream.read(limit).decode("utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def probe_media(path: Path) -> dict[str, Any] | None:
    """Inspect one file using FFprobe without loading media into memory."""
    executable = resolve_executable(FFPROBE)
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=width,height,duration,r_frame_rate,avg_frame_rate:format=duration", "-of", "json", str(path)],
                                capture_output=True, text=True, timeout=45, check=True)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            return None
        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            return None
        video = streams[0] if streams and isinstance(streams[0], dict) else None
        if video is None:
            return None
        format_info = payload.get("format")
        format_duration = format_info.get("duration") if isinstance(format_info, dict) else None
        duration = float(video.get("duration") or format_duration or 0)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        frame_rate = None
        for raw_rate in (video.get("avg_frame_rate"), video.get("r_frame_rate")):
            if isinstance(raw_rate, str) and len(raw_rate) <= 32:
                try:
                    numerator, separator, denominator = raw_rate.partition("/")
                    frame_rate = float(numerator) / (float(denominator) if separator else 1)
                    if not math.isfinite(frame_rate) or not 0 < frame_rate <= 240:
                        frame_rate = None
                except (ValueError, ZeroDivisionError):
                    frame_rate = None
            if frame_rate is not None:
                break
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_SOURCE_SECONDS:
            return None
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            return None
        audio_result = subprocess.run([executable, "-v", "error", "-select_streams", "a:0", "-show_entries",
                                       "stream=codec_type", "-of", "json", str(path)],
                                      capture_output=True, text=True, timeout=45, check=True)
        audio_payload = json.loads(audio_result.stdout)
        audio_streams = audio_payload.get("streams") if isinstance(audio_payload, dict) else None
        return {"duration": duration, "width": width, "height": height, "frame_rate": frame_rate,
                "has_audio": isinstance(audio_streams, list) and bool(audio_streams)}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None


def probe_output(path: Path) -> dict[str, Any] | None:
    """Reuse FFprobe validation for rendered output verification."""
    return probe_media(path)


class APIError(Exception):
    """Expected API failure with an HTTP status and safe user-facing detail."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def bounded_text(value: Any, label: str, minimum: int, maximum: int, default: str | None = None) -> str:
    """Validate an untrusted JSON string and return its trimmed form."""
    if value is None and default is not None:
        value = default
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise APIError(422, f"{label} must be {minimum}–{maximum} characters.")
    return value.strip()


def bounded_percent(value: Any, label: str, default: float = 50) -> float:
    """Validate a crop anchor as a finite percentage between the frame edges."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise APIError(422, f"{label} must be a number from 0 to 100.")
    return round(float(value), 2)


def json_object(raw: bytes) -> dict[str, Any]:
    """Decode a bounded JSON object and reject non-object request bodies."""
    if len(raw) > 256 * 1024:
        raise APIError(413, "JSON request exceeds the 256 KiB limit.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise APIError(400, "Request body must be valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise APIError(422, "Request body must be a JSON object.")
    return value


def health():
    """Report local dependencies and the fixed-disabled publishing state."""
    database_ok = False
    try:
        with connect() as db:
            database_ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        database_ok = False
    ffmpeg_path = resolve_executable(FFMPEG)
    ffprobe_path = resolve_executable(FFPROBE)
    return {"status": "ok" if database_ok else "degraded", "ffmpeg_available": bool(ffmpeg_path),
            "ffprobe_available": bool(ffprobe_path),
            "publishing_enabled": False, "ai_editing_available": False,
            "ai_editing_message": "No local AI model or analysis module is configured; edit choices are user-directed.",
            "database_ok": database_ok, "data_dir": str(DATA_ROOT)}


def list_creators():
    """List persisted creator identities without exposing credentials."""
    with connect() as db:
        return [row_dict(row) for row in db.execute("SELECT * FROM creators ORDER BY name COLLATE NOCASE")]


def create_creator(payload: dict[str, Any]):
    """Validate and persist a creator identity for source permission records."""
    name = bounded_text(payload.get("name"), "Creator name", 1, 120)
    source_url = bounded_text(payload.get("source_url"), "Source URL", 0, 500, "")
    if source_url:
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise APIError(422, "Creator URL must be an HTTP(S) URL without embedded credentials.")
    creator_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO creators(id,name,source_url,created_at) VALUES(?,?,?,?)",
                   (creator_id, name, source_url, now_iso()))
        event(db, "creator", creator_id, "registration", "recorded")
    return {"id": creator_id, "name": name, "source_url": source_url}


def list_assets():
    """List imported source metadata and rights state without exposing filesystem paths."""
    with connect() as db:
        rows = db.execute("""SELECT a.id,a.filename,a.sha256,a.size_bytes,a.duration,a.width,a.height,a.frame_rate,a.has_audio,
                                  a.creator_id,a.status,a.permission_status,a.permission_evidence,
                                  a.permission_expires_at,a.commercial_permission_status,
                                  a.commercial_permission_evidence,a.commercial_permission_expires_at,
                                  a.source_url,a.license_reference,a.attribution,a.restrictions,
                                  a.permission_checked_at,a.permission_version,a.imported_at,c.name creator_name
                             FROM assets a LEFT JOIN creators c ON c.id=a.creator_id
                             ORDER BY a.imported_at DESC""").fetchall()
        assets = [row_dict(row) for row in rows]
        for asset in assets:
            asset["permission_valid"] = classify_permission(asset["permission_status"], asset["permission_expires_at"])
            asset["commercial_permission_valid"] = classify_permission(
                asset["commercial_permission_status"], asset["commercial_permission_expires_at"])
        return assets


def local_source(asset_id: str) -> Path:
    """Resolve one imported source by its database ID without exposing storage paths."""
    with connect() as db:
        row = db.execute("SELECT relative_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        raise APIError(404, "Asset not found.")
    root = SOURCE_ROOT.resolve()
    path = (root / row["relative_path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise APIError(404, "Source media not found.")
    return path


def import_local_file(filename: str, body: BinaryIO, declared_length: str | None):
    """Stream a supported local upload into managed storage and record its hash and probe status."""
    try:
        name = safe_filename(filename)
    except ValueError as exc:
        raise APIError(422, str(exc)) from exc
    if declared_length is None:
        raise APIError(411, "A Content-Length header is required for local uploads.")
    if not re.fullmatch(r"[0-9]{1,12}", declared_length):
        raise APIError(400, "Invalid Content-Length header.")
    length = int(declared_length)
    if length > MAX_UPLOAD_BYTES:
        raise APIError(413, "File exceeds the configured size limit.")
    asset_id = str(uuid.uuid4())
    relative = f"{asset_id}{Path(name).suffix.lower()}"
    target = SOURCE_ROOT / relative
    temporary = SOURCE_ROOT / f".{asset_id}.part"
    digest = hashlib.sha256()
    size = 0
    remaining = length
    try:
        with temporary.open("xb") as stream:
            while remaining:
                chunk = body.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise APIError(400, "Upload ended before the declared Content-Length.")
                size += len(chunk)
                remaining -= len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise APIError(413, "File exceeds the configured size limit.")
                digest.update(chunk)
                stream.write(chunk)
        if size == 0:
            raise APIError(422, "The selected file is empty.")
        with SOURCE_COMMIT_LOCK:
            with connect() as db:
                duplicate = db.execute("SELECT id FROM assets WHERE sha256=?", (digest.hexdigest(),)).fetchone()
                if duplicate:
                    raise APIError(409, f"This exact file is already imported as {duplicate['id']}.")
            os.replace(temporary, target)
            metadata = probe_media(target)
            ffprobe_executable = resolve_executable(FFPROBE)
            if ffprobe_executable and not metadata:
                remove_generated_file(target, "invalid_import_copy")
                raise APIError(422, "FFprobe could not validate this file as supported video media.")
            status = "inspected" if metadata else "inspection_required"
            with connect() as db:
                db.execute("""INSERT INTO assets(id,filename,relative_path,sha256,size_bytes,duration,width,height,frame_rate,has_audio,
                             status,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (asset_id, name, relative, digest.hexdigest(), size,
                            metadata["duration"] if metadata else None, metadata["width"] if metadata else None,
                            metadata["height"] if metadata else None, metadata["frame_rate"] if metadata else None,
                            int(metadata["has_audio"]) if metadata else None,
                            status, now_iso()))
                event(db, "asset", asset_id, "import", "completed", f"bytes={size}; sha256={digest.hexdigest()}")
        return {"id": asset_id, "filename": name, "size_bytes": size, "sha256": digest.hexdigest(),
                "status": status, "metadata": metadata, "permission_status": "unverified"}
    except APIError:
        remove_generated_file(temporary, "upload_temporary")
        raise
    except OSError as exc:
        remove_generated_file(temporary, "upload_temporary")
        log.error("import failed asset=%s category=storage_error", asset_id)
        raise APIError(500, "The file could not be stored. Check disk space and folder permissions.") from exc


def inspect_asset(asset_id: str):
    """Inspect an already stored source after FFprobe is configured, without reimporting it."""
    executable = resolve_executable(FFPROBE)
    if not executable:
        raise APIError(503, "FFprobe is unavailable. Configure VSF_FFPROBE and try again.")
    with connect() as db:
        asset = db.execute("SELECT relative_path FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise APIError(404, "Asset not found.")
        path = SOURCE_ROOT / asset["relative_path"]
        metadata = probe_media(path)
        if not metadata:
            db.execute("UPDATE assets SET status='invalid_media' WHERE id=?", (asset_id,))
            event(db, "asset", asset_id, "inspection", "failed", "FFprobe rejected the media file.")
        else:
            db.execute("UPDATE assets SET duration=?,width=?,height=?,frame_rate=?,has_audio=?,status='inspected' WHERE id=?",
                       (metadata["duration"], metadata["width"], metadata["height"], metadata["frame_rate"],
                        int(metadata["has_audio"]), asset_id))
            event(db, "asset", asset_id, "inspection", "completed", json.dumps(metadata))
    if not metadata:
        raise APIError(422, "FFprobe could not validate this file as supported video media.")
    return {"id": asset_id, "status": "inspected", "metadata": metadata}


def record_permission(asset_id: str, payload: dict[str, Any]):
    """Record user-reviewed creator permission; evidence is not independently verified."""
    creator_id = bounded_text(payload.get("creator_id"), "Creator ID", 1, 100)
    status = payload.get("status")
    evidence = bounded_text(payload.get("evidence"), "Permission evidence", 3, 2000)
    expires_at = payload.get("expires_at")
    commercial_status = payload.get("commercial_status", "unverified")
    commercial_evidence = bounded_text(payload.get("commercial_evidence"), "Commercial permission evidence", 0, 2000, "")
    commercial_expires_at = payload.get("commercial_expires_at")
    source_url = bounded_text(payload.get("source_url"), "Original source URL", 0, 500, "")
    license_reference = bounded_text(payload.get("license_reference"), "License reference", 0, 500, "")
    attribution = bounded_text(payload.get("attribution"), "Attribution requirements", 0, 1000, "")
    restrictions = bounded_text(payload.get("restrictions"), "Editing or reuse restrictions", 0, 1000, "")
    if not isinstance(status, str) or status not in {"granted", "revoked", "rejected", "unverified"}:
        raise APIError(422, "Permission status must be granted, revoked, rejected, or unverified.")
    for field, value in (("Permission", expires_at), ("Commercial permission", commercial_expires_at)):
        if value is not None:
            if not isinstance(value, str):
                raise APIError(422, f"{field} expiry must be an ISO date/time string.")
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise APIError(422, f"{field} expiry must be an ISO date/time string.") from exc
    if commercial_status not in {"granted", "revoked", "rejected", "unverified"}:
        raise APIError(422, "Commercial permission status must be granted, revoked, rejected, or unverified.")
    if commercial_status == "granted" and len(commercial_evidence.strip()) < 3:
        raise APIError(422, "Evidence is required for a commercial-use permission grant.")
    if source_url:
        parsed = urlsplit(source_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise APIError(422, "Original source URL must be HTTP(S) without embedded credentials.")
    with connect() as db:
        if not db.execute("SELECT id FROM creators WHERE id=?", (creator_id,)).fetchone():
            raise APIError(404, "Register the creator before recording permission.")
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise APIError(404, "Asset not found.")
        version = asset["permission_version"] + 1
        db.execute("""UPDATE assets SET creator_id=?,permission_status=?,permission_evidence=?,permission_expires_at=?,
                     commercial_permission_status=?,commercial_permission_evidence=?,commercial_permission_expires_at=?,
                     source_url=?,license_reference=?,attribution=?,restrictions=?,permission_checked_at=?,permission_version=?
                     WHERE id=?""",
                   (creator_id, status, evidence, expires_at, commercial_status, commercial_evidence,
                    commercial_expires_at, source_url, license_reference, attribution, restrictions,
                    now_iso(), version, asset_id))
        db.execute("""INSERT INTO permission_history(asset_id,version,creator_id,permission_status,permission_evidence,
                     permission_expires_at,commercial_permission_status,commercial_permission_evidence,
                     commercial_permission_expires_at,source_url,license_reference,attribution,restrictions,recorded_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (asset_id, version, creator_id, status, evidence, expires_at, commercial_status,
                    commercial_evidence, commercial_expires_at, source_url, license_reference, attribution,
                    restrictions, now_iso()))
        event(db, "asset", asset_id, "permission", status, f"version={version}; {evidence}")
    return {"asset_id": asset_id, "permission_status": status, "permission_version": version,
            "commercial_permission_status": commercial_status}


def add_youtube_candidate(payload: dict[str, Any]):
    """Register a video link as a discovery lead; it never downloads video or implies reuse permission."""
    try:
        video_id = parse_youtube_video_url(payload.get("url"))
    except ValueError as exc:
        raise APIError(422, str(exc)) from exc
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    with connect() as db:
        existing = db.execute("SELECT * FROM youtube_candidates WHERE video_id=?", (video_id,)).fetchone()
        if existing:
            return row_dict(existing)
        candidate_id = str(uuid.uuid4())
        created_at = now_iso()
        db.execute("INSERT INTO youtube_candidates(id,video_id,url,status,created_at) VALUES(?,?,?,'media_required',?)",
                   (candidate_id, video_id, canonical_url, created_at))
        event(db, "youtube_candidate", candidate_id, "link_import", "media_required",
              "Video file and documented permission are required before editing.")
    return {"id": candidate_id, "video_id": video_id, "url": canonical_url,
            "status": "media_required", "created_at": created_at}


def list_youtube_candidates():
    """List local YouTube link references without remote retrieval or media data."""
    with connect() as db:
        return [row_dict(row) for row in db.execute(
            "SELECT id,video_id,url,status,created_at FROM youtube_candidates ORDER BY created_at DESC LIMIT 500")]


def _decode_project(row: sqlite3.Row) -> dict[str, Any]:
    """Return a project row with its durable edit-decision document decoded."""
    result = row_dict(row)
    try:
        result["plan"] = json.loads(result.pop("plan_json"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise APIError(409, "The saved project document is invalid. Restore it from project history.") from exc
    return result


def create_edit_project(asset_id: str, payload: dict[str, Any]):
    """Persist a version-one edit plan without modifying the imported source."""
    with connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise APIError(404, "Source asset not found.")
        rights_record = db.execute("SELECT id FROM permission_history WHERE asset_id=? AND version=?",
                                   (asset_id, asset["permission_version"])).fetchone()
        try:
            plan = build_edit_plan(asset_id, row_dict(asset), asset["permission_version"], payload,
                                   rights_record["id"] if rights_record else None)
        except (ValueError, TypeError) as exc:
            raise APIError(422, str(exc)) from exc
        project_id, now = str(uuid.uuid4()), now_iso()
        encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        db.execute("""INSERT INTO edit_projects(id,asset_id,title,schema_version,plan_json,current_version,
                     review_status,created_at,updated_at) VALUES(?,?,?,?,?,1,'draft',?,?)""",
                   (project_id, asset_id, plan["title"], plan["schema_version"], encoded, now, now))
        db.execute("INSERT INTO project_versions(project_id,version,plan_json,created_at) VALUES(?,1,?,?)",
                   (project_id, encoded, now))
        event(db, "edit_project", project_id, "project_save", "created", f"asset_id={asset_id}")
        row = db.execute("SELECT * FROM edit_projects WHERE id=?", (project_id,)).fetchone()
    return _decode_project(row)


def get_edit_project(project_id: str):
    """Load one durable edit plan for editor rehydration after a restart."""
    with connect() as db:
        row = db.execute("SELECT * FROM edit_projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise APIError(404, "Edit project not found.")
    return _decode_project(row)


def list_edit_projects(asset_id: str | None = None):
    """List a bounded set of saved projects, optionally for one source asset."""
    with connect() as db:
        if asset_id:
            rows = db.execute("SELECT * FROM edit_projects WHERE asset_id=? ORDER BY updated_at DESC LIMIT 200",
                              (asset_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM edit_projects ORDER BY updated_at DESC LIMIT 200").fetchall()
    return [_decode_project(row) for row in rows]


def list_edit_project_versions(project_id: str):
    """List immutable project snapshots without returning the current mutable row by reference."""
    with connect() as db:
        if not db.execute("SELECT 1 FROM edit_projects WHERE id=?", (project_id,)).fetchone():
            raise APIError(404, "Edit project not found.")
        rows = db.execute("SELECT version,plan_json,created_at FROM project_versions WHERE project_id=? ORDER BY version DESC LIMIT 50",
                          (project_id,)).fetchall()
    versions = []
    for row in rows:
        try:
            versions.append({"version": row["version"], "plan": json.loads(row["plan_json"]),
                             "created_at": row["created_at"]})
        except json.JSONDecodeError as exc:
            raise APIError(409, "A saved project version is invalid.") from exc
    return versions


def list_edit_project_reviews(project_id: str):
    """Return immutable human decisions for outputs made from this project and version."""
    with connect() as db:
        if not db.execute("SELECT 1 FROM edit_projects WHERE id=?", (project_id,)).fetchone():
            raise APIError(404, "Edit project not found.")
        return [row_dict(row) for row in db.execute(
            "SELECT * FROM project_reviews WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,))]


def update_edit_project(project_id: str, payload: dict[str, Any]):
    """Validate and append a new immutable edit-plan version while retaining prior snapshots."""
    with connect() as db:
        project = db.execute("SELECT * FROM edit_projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise APIError(404, "Edit project not found.")
        asset = db.execute("SELECT * FROM assets WHERE id=?", (project["asset_id"],)).fetchone()
        current = _decode_project(project)["plan"]
        merged = {"title": payload.get("title", current["title"]),
                  "segments": payload.get("segments", current["segments"]),
                  "profile": payload.get("profile", current["editing"]["profile"]),
                  "content_preset": payload.get("content_preset", current["editing"]["content_preset"]),
                  "preset": payload.get("preset", current["editing"]["visual_preset"]),
                  "crop_x": payload.get("crop_x", current["editing"]["crop"]["x"]),
                  "crop_y": payload.get("crop_y", current["editing"]["crop"]["y"]),
                  "captions_srt": payload.get("captions_srt", current["captions"]["text"]),
                  "caption_style": payload.get("caption_style", current["captions"]["style"]),
                  "caption_preset": payload.get("caption_preset", current["captions"]["preset"]),
                  "transcript_cues": payload.get("transcript_cues", current["transcript_cues"]),
                  "editorial_notes": payload.get("editorial_notes", current["editorial_notes"]),
                  "hook": payload.get("hook", current["hook"]),
                  "commercial_use": payload.get("commercial_use", current["commercial_use"])}
        rights_record = db.execute("SELECT id FROM permission_history WHERE asset_id=? AND version=?",
                                   (asset["id"], asset["permission_version"])).fetchone()
        try:
            plan = build_edit_plan(asset["id"], row_dict(asset), asset["permission_version"], merged,
                                   rights_record["id"] if rights_record else None)
        except (ValueError, TypeError) as exc:
            raise APIError(422, str(exc)) from exc
        version, now = project["current_version"] + 1, now_iso()
        encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        db.execute("UPDATE edit_projects SET title=?,schema_version=?,plan_json=?,current_version=?,review_status='draft',review_notes='',updated_at=? WHERE id=?",
                   (plan["title"], plan["schema_version"], encoded, version, now, project_id))
        db.execute("INSERT INTO project_versions(project_id,version,plan_json,created_at) VALUES(?,?,?,?)",
                   (project_id, version, encoded, now))
        event(db, "edit_project", project_id, "project_save", "updated", f"version={version}")
        row = db.execute("SELECT * FROM edit_projects WHERE id=?", (project_id,)).fetchone()
    return _decode_project(row)


def _run_analysis_filter(executable: str, source: Path, duration: float, *, video: bool,
                         filter_value: str, timeout_seconds: int = 180) -> tuple[int | None, str]:
    """Run one bounded FFmpeg analysis pass, stopping if diagnostics exceed the memory-safe cap."""
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "info", "-t", str(duration), "-i", str(source)]
    command.extend(["-vf", filter_value, "-an"] if video else ["-vn", "-af", filter_value])
    command.extend(["-f", "null", "-"])
    try:
        with tempfile.TemporaryFile(mode="w+b") as diagnostics:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=diagnostics)
            deadline, output_limited = time.monotonic() + timeout_seconds, False
            while process.poll() is None:
                if diagnostics.tell() > 4 * 1024 * 1024:
                    output_limited = True
                    process.terminate()
                    break
                if time.monotonic() >= deadline:
                    process.terminate()
                    break
                time.sleep(0.1)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            diagnostics.seek(max(0, diagnostics.tell() - 4 * 1024 * 1024))
            text = diagnostics.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
            return (None if output_limited or time.monotonic() >= deadline else process.returncode), text
    except (OSError, subprocess.SubprocessError) as exc:
        return None, type(exc).__name__


def analyze_source(source: Path, duration: float, has_audio: bool | None,
                   scene_threshold: float = 0.35) -> dict[str, Any]:
    """Collect only scene and silence timestamps using low-resolution CPU-friendly FFmpeg filters."""
    executable = resolve_executable(FFMPEG)
    if not executable:
        return {"scene_times": [], "silence_intervals": [],
                "diagnostics": ["FFmpeg is unavailable; evenly sampled review windows were generated."]}
    scene_filter = f"fps=2,scale=320:-2,select=gt(scene\\,{scene_threshold:.2f}),showinfo"
    scene_code, scene_log = _run_analysis_filter(executable, source, duration, video=True,
                                                 filter_value=scene_filter)
    scene_times = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", scene_log)]
    diagnostics = []
    if scene_code != 0:
        scene_times = []
        diagnostics.append("Scene-change filter unavailable or analysis exceeded its resource limit; using fallback windows.")
    silence_intervals = []
    if has_audio:
        silence_code, silence_log = _run_analysis_filter(executable, source, duration, video=False,
                                                         filter_value="silencedetect=noise=-35dB:d=0.8")
        silence_events = [(float(value), "start") for value in re.findall(
            r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", silence_log)]
        silence_events.extend((float(value), "end") for value in re.findall(
            r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", silence_log))
        open_silence = None
        for moment, event_name in sorted(silence_events):
            if event_name == "start":
                open_silence = moment
            elif open_silence is not None and moment > open_silence:
                silence_intervals.append((open_silence, min(moment, duration)))
                open_silence = None
        if open_silence is not None:
            silence_intervals.append((open_silence, duration))
        if silence_code != 0:
            diagnostics.append("Silence detection is unavailable or exceeded its resource limit; no silence signal was used.")
    elif has_audio is None:
        diagnostics.append("Audio status is unknown; silence detection was skipped until source inspection succeeds.")
    return {"scene_times": scene_times[:10000], "silence_intervals": silence_intervals[:10000],
            "diagnostics": diagnostics}


CONTENT_PRESETS = {
    "gaming": {"minimum_seconds": 8, "maximum_seconds": 28, "padding_before": 1.5, "padding_after": 2.5},
    "podcast": {"minimum_seconds": 15, "maximum_seconds": 45, "padding_before": 4, "padding_after": 5},
    "educational": {"minimum_seconds": 15, "maximum_seconds": 50, "padding_before": 5, "padding_after": 5},
    "cinematic": {"minimum_seconds": 18, "maximum_seconds": 45, "padding_before": 6, "padding_after": 6},
    "creator_commentary": {"minimum_seconds": 10, "maximum_seconds": 35, "padding_before": 2, "padding_after": 3},
    "custom": {"minimum_seconds": 10, "maximum_seconds": 30, "padding_before": 3, "padding_after": 3},
}


def discover_candidates(asset_id: str, payload: dict[str, Any]):
    """Analyze an authorized local source and persist heuristic candidates as pending human reviews."""
    preset = payload.get("content_preset", "custom")
    if preset not in CONTENT_PRESETS:
        raise APIError(422, "Choose a supported content preset.")
    settings = CONTENT_PRESETS[preset].copy()
    for name in settings:
        if name in payload:
            value = payload[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise APIError(422, "Candidate settings must be finite numbers.")
            settings[name] = value
    scene_threshold = payload.get("scene_threshold", 0.35)
    if (isinstance(scene_threshold, bool) or not isinstance(scene_threshold, (int, float))
            or not math.isfinite(scene_threshold) or not 0.1 <= scene_threshold <= 0.9):
        raise APIError(422, "Scene-change sensitivity must be from 0.1 to 0.9.")
    transcript_cues = payload.get("transcript_cues", [])
    if not isinstance(transcript_cues, list) or len(transcript_cues) > 1000:
        raise APIError(422, "Source transcript may contain at most 1,000 timed cues.")
    with connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise APIError(404, "Source asset not found.")
        if not asset["duration"]:
            raise APIError(409, "Inspect the source before discovering candidate clips.")
        if not classify_permission(asset["permission_status"], asset["permission_expires_at"]):
            raise APIError(403, "A current editing permission record is required for local analysis.")
        source = (SOURCE_ROOT / asset["relative_path"]).resolve()
        if SOURCE_ROOT.resolve() not in source.parents or not source.is_file():
            raise APIError(404, "Source media is missing from managed storage.")
    try:
        windows = generate_candidate_windows(asset["duration"], [], [], settings["minimum_seconds"],
                                             settings["maximum_seconds"], settings["padding_before"],
                                             settings["padding_after"])
    except ValueError as exc:
        raise APIError(422, str(exc)) from exc
    analysis = analyze_source(source, asset["duration"], asset["has_audio"], float(scene_threshold))
    try:
        windows = generate_candidate_windows(asset["duration"], analysis["scene_times"],
                                             analysis["silence_intervals"], settings["minimum_seconds"],
                                             settings["maximum_seconds"], settings["padding_before"],
                                             settings["padding_after"], transcript_cues)
    except ValueError as exc:
        raise APIError(422, str(exc)) from exc
    now, created = now_iso(), []
    with connect() as db:
        for item in windows:
            candidate_id = str(uuid.uuid4())
            db.execute("""INSERT INTO clip_candidates(id,asset_id,start_seconds,end_seconds,score,signals_json,
                         reasons_json,warnings_json,score_factors_json,status,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?, 'pending',?,?)""",
                       (candidate_id, asset_id, item["start_seconds"], item["end_seconds"], item["score"],
                        json.dumps(item["signals"]), json.dumps(item["reasons"]), json.dumps(item["warnings"]),
                        json.dumps(item["score_factors"]), now, now))
            event(db, "clip_candidate", candidate_id, "discovery", "pending", "; ".join(item["reasons"]))
            created.append({"id": candidate_id, **item, "status": "pending", "project_id": None,
                            "asset_id": asset_id, "created_at": now})
    return {"asset_id": asset_id, "content_preset": preset, "candidates": created,
            "diagnostics": analysis["diagnostics"], "scoring_note": "Heuristic review ranking only; it does not predict quality or virality."}


def list_candidates(asset_id: str | None = None):
    """Return bounded persisted candidate records, optionally filtered to one source."""
    with connect() as db:
        rows = (db.execute("SELECT * FROM clip_candidates WHERE asset_id=? ORDER BY created_at DESC LIMIT 500", (asset_id,)).fetchall()
                if asset_id else db.execute("SELECT * FROM clip_candidates ORDER BY created_at DESC LIMIT 500").fetchall())
    result = []
    for row in rows:
        item = row_dict(row)
        for field, target in (("signals_json", "signals"), ("reasons_json", "reasons"),
                              ("warnings_json", "warnings"), ("score_factors_json", "score_factors")):
            item[target] = json.loads(item.pop(field))
        result.append(item)
    return result


def update_candidate(candidate_id: str, payload: dict[str, Any]):
    """Record an explicit accept/reject decision and create an editable project for accepted clips."""
    decision = payload.get("decision")
    if decision not in {"accepted", "rejected"}:
        raise APIError(422, "Candidate decision must be accepted or rejected.")
    with connect() as db:
        candidate = db.execute("SELECT * FROM clip_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            raise APIError(404, "Candidate not found.")
        if candidate["status"] != "pending":
            raise APIError(409, "This candidate already has a human decision.")
        notes = bounded_text(payload.get("notes"), "Candidate notes", 0, 1000, "")
        title = bounded_text(payload.get("title"), "Candidate title", 0, 120, "")
        hook = bounded_text(payload.get("hook"), "Candidate hook", 0, 240, "")
        now, project_id = now_iso(), None
        start, end = candidate["start_seconds"], candidate["end_seconds"]
        if decision == "accepted":
            raw_start, raw_end = payload.get("start_seconds", start), payload.get("end_seconds", end)
            with connect() as asset_db:
                asset = asset_db.execute("SELECT * FROM assets WHERE id=?", (candidate["asset_id"],)).fetchone()
                rights = asset_db.execute("SELECT id FROM permission_history WHERE asset_id=? AND version=?",
                                          (asset["id"], asset["permission_version"])).fetchone()
            try:
                start, end = validate_cut(raw_start, raw_end, asset["duration"], None)
                plan = build_edit_plan(asset["id"], row_dict(asset), asset["permission_version"],
                    {"title": title or "Candidate edit", "segments": [{"start_seconds": start,
                     "end_seconds": end, "candidate_id": candidate_id}], "profile": payload.get("profile", "short_vertical"),
                     "content_preset": payload.get("content_preset", "custom"), "preset": payload.get("preset", "clean_vertical"),
                     "caption_preset": payload.get("caption_preset"), "caption_style": payload.get("caption_style"),
                     "captions_srt": payload.get("captions_srt", ""), "editorial_notes": notes,
                     "hook": hook, "transcript_cues": payload.get("transcript_cues", [])},
                     rights["id"] if rights else None)
            except (ValueError, TypeError) as exc:
                raise APIError(422, str(exc)) from exc
            project_id = str(uuid.uuid4())
            encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
            db.execute("""INSERT INTO edit_projects(id,asset_id,title,schema_version,plan_json,current_version,
                         review_status,review_notes,created_at,updated_at) VALUES(?,?,?,?,?,1,'draft',?,?,?)""",
                       (project_id, asset["id"], plan["title"], plan["schema_version"], encoded,
                        f"Candidate hook: {hook}" if hook else "", now, now))
            db.execute("INSERT INTO project_versions(project_id,version,plan_json,created_at) VALUES(?,1,?,?)",
                       (project_id, encoded, now))
            event(db, "edit_project", project_id, "project_save", "candidate_accepted", f"candidate_id={candidate_id}")
        db.execute("""UPDATE clip_candidates SET status=?,title=?,hook=?,notes=?,start_seconds=?,end_seconds=?,
                     project_id=?,updated_at=? WHERE id=? AND status='pending'""",
                   (decision, title, hook, notes, start, end, project_id, now, candidate_id))
        event(db, "clip_candidate", candidate_id, "human_review", decision, notes)
        row = db.execute("SELECT * FROM clip_candidates WHERE id=?", (candidate_id,)).fetchone()
    item = row_dict(row)
    for field, target in (("signals_json", "signals"), ("reasons_json", "reasons"),
                          ("warnings_json", "warnings"), ("score_factors_json", "score_factors")):
        item[target] = json.loads(item.pop(field))
    return item


def queue_render(asset_id: str, payload: dict[str, Any]):
    """Validate rights and cut bounds, persist a job, and queue its single-worker render."""
    with JOB_QUEUE_LOCK:
        return _queue_render_locked(asset_id, payload)


def _queue_render_locked(asset_id: str, payload: dict[str, Any], project_id: str | None = None,
                         project_version: int | None = None, expected_rights_version: int | None = None):
    profile_name = payload.get("profile", "short_vertical")
    try:
        profile = render_profile(profile_name)
    except ValueError as exc:
        raise APIError(422, str(exc)) from exc
    commercial_use = payload.get("commercial_use", False)
    if not isinstance(commercial_use, bool):
        raise APIError(422, "Commercial-use selection must be true or false.")
    preset = payload.get("preset", "clean_vertical")
    if not isinstance(preset, str) or preset not in {"clean_vertical", "punchline", "reaction"}:
        raise APIError(422, "Choose a supported editing preset.")
    editorial_notes = bounded_text(payload.get("editorial_notes"), "Editorial notes", 0, 2000, "")
    crop_x = bounded_percent(payload.get("crop_x"), "Horizontal crop position")
    crop_y = bounded_percent(payload.get("crop_y"), "Vertical crop position")
    try:
        caption_style = normalize_caption_style(payload.get("caption_style"))
    except ValueError as exc:
        raise APIError(422, str(exc)) from exc
    caption_style_json = json.dumps(caption_style, separators=(",", ":"))
    with connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise APIError(404, "Asset not found.")
        if expected_rights_version is not None and asset["permission_version"] != expected_rights_version:
            raise APIError(403, "This project was saved against an older rights record. Save a new project version after reviewing current permissions.")
        if not classify_permission(asset["permission_status"], asset["permission_expires_at"]):
            raise APIError(403, "A current creator permission record is required before rendering.")
        if commercial_use and not classify_permission(asset["commercial_permission_status"],
                                                      asset["commercial_permission_expires_at"]):
            raise APIError(403, "A separate current commercial-use permission record is required for this render.")
        if asset["duration"] is None:
            raise APIError(409, "Media duration is unavailable. Configure FFprobe and inspect this file before rendering.")
        raw_segments = payload.get("segments")
        if raw_segments is None:
            raw_segments = [{"start_seconds": payload.get("start_seconds"),
                             "end_seconds": payload.get("end_seconds")}]
        if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 100:
            raise APIError(422, "A render needs between 1 and 100 ordered timeline segments.")
        segments, total_duration = [], 0.0
        for index, item in enumerate(raw_segments):
            if not isinstance(item, dict):
                raise APIError(422, "Every timeline segment must be an object.")
            try:
                clip_start, clip_end = validate_cut(item.get("start_seconds"), item.get("end_seconds"),
                                                    asset["duration"], None)
            except ValueError as exc:
                raise APIError(422, f"Timeline segment {index + 1}: {exc}") from exc
            total_duration += clip_end - clip_start
            segments.append({"order": index, "start_seconds": clip_start, "end_seconds": clip_end})
        if profile["max_seconds"] is not None and total_duration > profile["max_seconds"]:
            raise APIError(422, f"The combined Shorts timeline must be no longer than {profile['max_seconds']} seconds.")
        start, end = segments[0]["start_seconds"], segments[-1]["end_seconds"]
        timeline_json = json.dumps(segments, separators=(",", ":"))
        try:
            captions_srt = validate_caption_timeline(payload.get("captions_srt", ""), total_duration,
                caption_style, 100 if profile_name == "short_vertical" else 2000)
        except ValueError as exc:
            raise APIError(422, str(exc)) from exc
        duplicate = db.execute("""SELECT id FROM jobs WHERE asset_id=? AND status IN ('queued','running')
                                 AND start_seconds=? AND end_seconds=? AND preset=? AND editorial_notes=? AND captions_srt=?
                                 AND caption_style_json=? AND crop_x=? AND crop_y=? AND profile=? AND commercial_use=? AND timeline_json=?
                                 AND project_id IS ? AND project_version IS ?
                                 ORDER BY created_at DESC LIMIT 1""",
                               (asset_id, start, end, preset, editorial_notes, captions_srt, caption_style_json, crop_x, crop_y,
                                profile_name, int(commercial_use), timeline_json, project_id, project_version)).fetchone()
        if duplicate:
            return {"id": duplicate["id"], "status": "queued", "already_queued": True, "publishing_enabled": False}
        pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if pending >= MAX_PENDING_JOBS:
            raise APIError(429, f"The local queue is full ({MAX_PENDING_JOBS} pending jobs). Wait for a job to finish or cancel one.")
        job_id = str(uuid.uuid4())
        now = now_iso()
        manifest = {"asset_id": asset_id, "rights_version": asset["permission_version"], "profile": profile_name,
                    "canvas": {"width": profile["width"], "height": profile["height"]}, "start_seconds": start,
                    "end_seconds": end, "segments": segments, "preset": preset, "crop_x": crop_x, "crop_y": crop_y,
                    "caption_style": caption_style,
                    "captions_sha256": hashlib.sha256(captions_srt.encode("utf-8")).hexdigest() if captions_srt else None,
                    "commercial_use": commercial_use}
        db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,editorial_notes,
                     captions_srt,caption_style_json,crop_x,crop_y,profile,rights_version,commercial_use,edit_manifest,timeline_json,
                     project_id,project_version,created_at,updated_at)
                     VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (job_id, asset_id, "queued", start, end, preset, editorial_notes, captions_srt, caption_style_json,
                    crop_x, crop_y, profile_name, asset["permission_version"], int(commercial_use), json.dumps(manifest), timeline_json,
                    project_id, project_version, now, now))
        event(db, "job", job_id, "render", "queued")
    start_render_worker(job_id)
    return {"id": job_id, "status": "queued", "publishing_enabled": False}


def queue_project_render(project_id: str):
    """Queue an ordered saved timeline after checking its saved rights-record version."""
    with JOB_QUEUE_LOCK:
        with connect() as db:
            project = db.execute("SELECT * FROM edit_projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise APIError(404, "Edit project not found.")
            plan = _decode_project(project)["plan"]
        editing = plan["editing"]
        payload = {"profile": editing["profile"], "preset": editing["visual_preset"],
                   "crop_x": editing["crop"]["x"], "crop_y": editing["crop"]["y"],
                   "commercial_use": plan["commercial_use"], "editorial_notes": plan["editorial_notes"],
                   "captions_srt": plan["captions"]["text"], "caption_style": plan["captions"]["style"],
                   "caption_preset": plan["captions"]["preset"],
                   "segments": plan["segments"]}
        result = _queue_render_locked(project["asset_id"], payload, project_id, project["current_version"],
                                      plan["source"]["rights_version"])
    return result


def retry_job(job_id: str):
    """Retry only a failed job explicitly marked safe, with current rights and a bounded retry count."""
    with connect() as db:
        job = db.execute("""SELECT j.*,a.permission_status,a.permission_expires_at,a.permission_version,
                            a.commercial_permission_status,a.commercial_permission_expires_at,a.relative_path
                            FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.id=?""", (job_id,)).fetchone()
        if not job:
            raise APIError(404, "Job not found.")
        if job["status"] != "failed" or not job["retry_safe"] or job["retry_count"] >= 3:
            raise APIError(409, "This job is not eligible for another safe retry.")
        if (not classify_permission(job["permission_status"], job["permission_expires_at"])
                or job["rights_version"] != job["permission_version"]
                or (job["commercial_use"] and not classify_permission(
                    job["commercial_permission_status"], job["commercial_permission_expires_at"]))):
            raise APIError(403, "Current editing and applicable commercial rights are required before retrying.")
        if not (SOURCE_ROOT / job["relative_path"]).is_file():
            raise APIError(409, "The original managed source file is missing.")
        changed = db.execute("""UPDATE jobs SET status='queued',stage='retry',error_category=NULL,diagnostic=NULL,
                             retry_safe=0,cancel_requested=0,retry_count=retry_count+1,updated_at=? WHERE id=? AND status='failed'""",
                             (now_iso(), job_id)).rowcount
        if not changed:
            raise APIError(409, "The job changed while the retry was being queued.")
        event(db, "job", job_id, "retry", "queued", f"retry_count={job['retry_count'] + 1}")
    start_render_worker(job_id, retry=True)
    return {"id": job_id, "status": "queued", "retry_count": job["retry_count"] + 1}


def cancel_job(job_id: str):
    """Cancel queued work immediately or request termination of its active FFmpeg process."""
    process = None
    with connect() as db:
        job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise APIError(404, "Job not found.")
        if job["status"] == "queued":
            db.execute("UPDATE jobs SET status='cancelled',stage='cancelled',updated_at=? WHERE id=? AND status='queued'",
                       (now_iso(), job_id))
            event(db, "job", job_id, "cancel", "cancelled", "Cancelled before rendering started.")
            return {"id": job_id, "status": "cancelled"}
        if job["status"] != "running":
            raise APIError(409, "Only queued or running jobs can be cancelled.")
        db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=? AND status='running'",
                   (now_iso(), job_id))
        event(db, "job", job_id, "cancel", "requested", "Waiting for the active render process to stop safely.")
    with PROCESS_LOCK:
        process = ACTIVE_PROCESSES.get(job_id)
    if process and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            log.warning("render cancellation signal failed job=%s", job_id)
    return {"id": job_id, "status": "cancelling"}


def cancellation_requested(job_id: str) -> bool:
    """Read the durable cancel flag so worker transitions survive API-thread races."""
    with connect() as db:
        row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row and row[0])


def finish_cancelled_render(job_id: str, detail: str) -> None:
    """Persist one terminal cancellation state after FFmpeg has stopped and partial output is removed."""
    with connect() as db:
        db.execute("""UPDATE jobs SET status='cancelled',stage='cancelled',cancel_requested=0,error_category=NULL,
                    diagnostic=?,retry_safe=0,updated_at=? WHERE id=?""",
                   (detail[:500], now_iso(), job_id))
        event(db, "job", job_id, "cancel", "cancelled", detail)


def run_render_process(job_id: str, command: list[str], log_path: Path, timeout: float) -> None:
    """Run FFmpeg without shell parsing or unbounded output capture, and register it for safe cancellation."""
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8", newline="\n") as log_stream:
        process = subprocess.Popen(command, cwd=OUTPUT_ROOT, stdout=log_stream, stderr=subprocess.STDOUT,
                                   text=True, creationflags=creationflags)
        with PROCESS_LOCK:
            ACTIVE_PROCESSES[job_id] = process
        try:
            if cancellation_requested(job_id) and process.poll() is None:
                process.terminate()
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise
        finally:
            with PROCESS_LOCK:
                ACTIVE_PROCESSES.pop(job_id, None)
    if cancellation_requested(job_id):
        return
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def start_render_worker(job_id: str, retry: bool = False) -> None:
    """Start a daemon worker or persist a retryable worker-start failure."""
    label = "retry" if retry else "render"
    try:
        threading.Thread(target=render_job, args=(job_id,), name=f"{label}-{job_id[:8]}", daemon=True).start()
    except (RuntimeError, OSError):
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',stage='worker_start',error_category='worker_unavailable',diagnostic='The background worker could not start.',retry_safe=1,updated_at=? WHERE id=?",
                       (now_iso(), job_id))
            event(db, "job", job_id, "worker_start", "failed", "Safe user-initiated retry is available.")


def render_job(job_id: str) -> None:
    """Render one authorized source into a vertical MP4 and verify it before review."""
    with RENDER_LOCK:
        _render_job(job_id)


def _render_job(job_id: str) -> None:
    with connect() as db:
        job = db.execute("""SELECT j.*,a.relative_path,a.has_audio,a.permission_status,a.permission_expires_at,
                            a.commercial_permission_status,a.commercial_permission_expires_at,a.permission_version
                            FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.id=?""", (job_id,)).fetchone()
        if not job:
            return
        if job["status"] not in {"queued", "running"}:
            return
        if job["cancel_requested"]:
            finish_cancelled_render(job_id, "Cancelled before FFmpeg started.")
            return
        if (not classify_permission(job["permission_status"], job["permission_expires_at"])
                or job["rights_version"] != job["permission_version"]
                or (job["commercial_use"] and not classify_permission(
                    job["commercial_permission_status"], job["commercial_permission_expires_at"]))):
            db.execute("UPDATE jobs SET status='failed',stage='permission_gate',error_category='permission',updated_at=? WHERE id=?", (now_iso(), job_id))
            event(db, "job", job_id, "permission_gate", "failed", "Permission missing, expired, or revoked.")
            return
        root = SOURCE_ROOT.resolve()
        source = (root / job["relative_path"]).resolve()
        if root not in source.parents or not source.is_file():
            db.execute("UPDATE jobs SET status='failed',stage='source_validation',error_category='missing_source',diagnostic='The managed source file is missing.',updated_at=? WHERE id=?", (now_iso(), job_id))
            event(db, "job", job_id, "source_validation", "failed", "Managed source file missing or outside source storage.")
            return
    executable = resolve_executable(FFMPEG)
    if not executable:
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',stage='render',error_category='missing_dependency',diagnostic='FFmpeg was not found.',retry_safe=1,updated_at=? WHERE id=?", (now_iso(), job_id))
            event(db, "job", job_id, "render", "failed", "FFmpeg missing")
        return
    profile = render_profile(job["profile"])
    width, height = profile["width"], profile["height"]
    output = OUTPUT_ROOT / f"{job_id}_{job['profile']}.mp4"
    temporary_output = OUTPUT_ROOT / f".{uuid.uuid4()}.partial.mp4"
    finalized_output = False
    output_reserved = False
    try:
        segments = json.loads(job["timeline_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        segments = []
    if not segments:
        segments = [{"start_seconds": job["start_seconds"], "end_seconds": job["end_seconds"], "order": 0}]
    duration = sum(segment["end_seconds"] - segment["start_seconds"] for segment in segments)
    anchor_x = job["crop_x"] / 100
    anchor_y = job["crop_y"] / 100
    crop_filter = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                   f"crop={width}:{height}:x=(in_w-{width})*{anchor_x}:y=(in_h-{height})*{anchor_y},setsar=1")
    zoom = {"clean_vertical": None, "punchline": 0.88, "reaction": 0.94}[job["preset"]]
    if zoom is not None:
        crop_filter += f",crop=iw*{zoom}:ih*{zoom},scale={width}:{height},setsar=1"
    caption_path = OUTPUT_ROOT / f"{job_id}.srt"
    render_log = LOG_ROOT / f"{job_id}.log"
    try:
        caption_style = normalize_caption_style(json.loads(job["caption_style_json"] or "{}"))
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        caption_style = normalize_caption_style(None)
        log.warning("invalid persisted caption style; using readable defaults job=%s category=%s", job_id, type(exc).__name__)
    output_font, caption_font_fallback = resolve_caption_font(caption_style["font_name"])
    filter_caption_style = {**caption_style, "font_name": output_font}
    caption_filter = (f"subtitles=filename='{caption_path.name}':force_style='{caption_filter_style(filter_caption_style)}'")
    command = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if len(segments) == 1:
        command.extend(["-ss", str(segments[0]["start_seconds"]), "-i", str(source),
                        "-t", str(duration), "-map", "0:v:0"])
        if job["has_audio"]:
            command.extend(["-map", "0:a?"])
        command.extend(["-vf", crop_filter + (f",{caption_filter}" if job["captions_srt"] else "")])
    else:
        filter_parts, video_labels, audio_labels = [], [], []
        for index, segment in enumerate(segments):
            segment_duration = segment["end_seconds"] - segment["start_seconds"]
            command.extend(["-ss", str(segment["start_seconds"]), "-t", str(segment_duration), "-i", str(source)])
            filter_parts.append(f"[{index}:v:0]trim=duration={segment_duration:.6f},setpts=PTS-STARTPTS,"
                                f"{crop_filter}[v{index}]")
            video_labels.append(f"[v{index}]")
            if job["has_audio"]:
                filter_parts.append(f"[{index}:a:0]atrim=duration={segment_duration:.6f},asetpts=PTS-STARTPTS[a{index}]")
                audio_labels.append(f"[a{index}]")
        filter_parts.append(f"{''.join(video_labels)}concat=n={len(segments)}:v=1:a=0[vconcat]")
        video_output = "[vconcat]"
        if job["captions_srt"]:
            filter_parts.append(f"[vconcat]{caption_filter}[vout]")
            video_output = "[vout]"
        command.extend(["-filter_complex", ";".join(filter_parts), "-map", video_output])
        if job["has_audio"]:
            filter_parts.append(f"{''.join(audio_labels)}concat=n={len(segments)}:v=0:a=1[aout]")
            command[command.index("-filter_complex") + 1] = ";".join(filter_parts)
            command.extend(["-map", "[aout]"])
    command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"])
    if job["has_audio"]:
        command.extend(["-c:a", "aac", "-b:a", "128k"])
    command.extend(["-movflags", "+faststart", str(temporary_output)])
    with connect() as db:
        db.execute("UPDATE jobs SET status='running',stage='render',updated_at=? WHERE id=?", (now_iso(), job_id))
    try:
        if job["captions_srt"]:
            rendered_captions = (wrap_srt_lines(job["captions_srt"], caption_style["max_line_length"])
                                 if caption_style["wrap"] else job["captions_srt"])
            validate_caption_timeline(rendered_captions, duration, {**caption_style, "wrap": False},
                                      100 if job["profile"] == "short_vertical" else 2000)
            caption_path.write_text(rendered_captions, encoding="utf-8", newline="\n")
        if output.exists():
            raise RuntimeError("Output filename collision; existing draft was preserved.")
        estimated_bytes = max(32 * 1024 * 1024, int(duration * 5_000_000 / 8 * 1.2))
        free_bytes = shutil.disk_usage(OUTPUT_ROOT).free
        if free_bytes < estimated_bytes:
            raise RuntimeError(f"Not enough free disk space. Need about {estimated_bytes // (1024 * 1024)} MiB; "
                               f"only {free_bytes // (1024 * 1024)} MiB is free.")
        run_render_process(job_id, command, render_log, min(24 * 60 * 60, max(600, duration * 20)))
        if cancellation_requested(job_id):
            remove_generated_file(temporary_output, "cancelled_render_output")
            finish_cancelled_render(job_id, "Render cancelled by the user.")
            return
        if not temporary_output.is_file() or temporary_output.stat().st_size <= 0:
            raise RuntimeError("FFmpeg produced an empty or missing temporary output.")
        metadata = probe_output(temporary_output)
        if (not metadata or metadata["width"] != width or metadata["height"] != height
                or metadata["duration"] > duration + 1 or metadata["duration"] < duration - 1
                or (job["has_audio"] and not metadata["has_audio"])):
            raise RuntimeError("Output verification failed.")
        reservation = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(reservation)
        output_reserved = True
        os.replace(temporary_output, output)
        finalized_output = True
        output_reserved = False
        manifest = json.loads(job["edit_manifest"])
        manifest["render_parameters"] = {"video_codec": "libx264", "video_preset": "veryfast", "crf": 22,
                                         "pixel_format": "yuv420p", "audio_codec": "aac" if job["has_audio"] else None,
                                         "filter": crop_filter}
        warnings = [] if job["has_audio"] else ["Source has no audio track; rendered without audio."]
        if job["captions_srt"] and caption_font_fallback:
            warnings.append(f"Preferred caption font was unavailable; {output_font} fallback was used.")
        checks = [
            {"name": "output_file_nonempty", "status": "PASS"},
            {"name": "video_stream_probe", "status": "PASS"},
            {"name": "profile_resolution", "status": "PASS"},
            {"name": "duration_within_cut", "status": "PASS"},
            {"name": "source_audio_preserved", "status": "PASS" if job["has_audio"] else "WARN",
             "detail": "Source contains no audio track." if not job["has_audio"] else ""},
            {"name": "caption_syntax", "status": "PASS" if job["captions_srt"] else "NOT_APPLICABLE"},
            {"name": "caption_wrapping", "status": "PASS" if job["captions_srt"] and caption_style["wrap"] else "NOT_APPLICABLE"},
            {"name": "caption_font", "status": "WARN" if job["captions_srt"] and caption_font_fallback else "PASS" if job["captions_srt"] else "NOT_APPLICABLE",
             "detail": output_font if job["captions_srt"] else ""},
        ]
        report = {"valid_video": True, "quality_status": "WARN" if warnings else "PASS", "checks": checks,
                  "width": metadata["width"], "height": metadata["height"],
                  "duration": metadata["duration"], "has_audio": metadata["has_audio"],
                  "silent_audio_allowed": not job["has_audio"],
                  "warnings": warnings,
                  "profile": job["profile"], "rights_version": job["rights_version"],
                  "manifest": manifest}
        with connect() as db:
            changed = db.execute("UPDATE jobs SET status='review',stage='quality_assurance',output_path=?,quality_report=?,edit_manifest=?,log_path=?,updated_at=? WHERE id=? AND status='running' AND cancel_requested=0",
                                 (output.name, json.dumps(report), json.dumps(manifest), render_log.name, now_iso(), job_id)).rowcount
            if not changed:
                raise RuntimeError("Render was cancelled before quality approval.")
            event(db, "job", job_id, "quality_assurance", "passed", json.dumps(report))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        if cancellation_requested(job_id):
            if finalized_output:
                remove_generated_file(output, "cancelled_render_output")
            remove_generated_file(temporary_output, "cancelled_partial_output")
            finish_cancelled_render(job_id, "Render cancelled by the user; partial output was discarded.")
            return
        if finalized_output:
            remove_generated_file(output, "failed_render_output")
        elif output_reserved:
            remove_generated_file(output, "failed_output_reservation")
        remove_generated_file(temporary_output, "partial_render_output")
        log_diagnostic = read_log_tail(render_log, 500)
        diagnostic = log_diagnostic if log_diagnostic.strip() else str(exc)[:500]
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',stage='render',error_category='render_failure',diagnostic=?,log_path=?,retry_safe=0,updated_at=? WHERE id=?",
                       (diagnostic, render_log.name if render_log.exists() else None, now_iso(), job_id))
            event(db, "job", job_id, "render", "failed", "FFmpeg or output validation failed.")
    finally:
        remove_generated_file(caption_path, "caption_temporary")


def list_jobs():
    """Return persisted render and review states for the workstation."""
    with connect() as db:
        rows = db.execute("""SELECT j.*,a.filename FROM jobs j JOIN assets a ON a.id=j.asset_id ORDER BY j.created_at DESC""").fetchall()
    return [row_dict(row) for row in rows]


def get_job(job_id: str):
    """Return the persisted state of one job for bounded UI progress checks."""
    with connect() as db:
        row = db.execute("SELECT j.*,a.filename FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.id=?", (job_id,)).fetchone()
    if not row:
        raise APIError(404, "Job not found.")
    return row_dict(row)


def review_job(job_id: str, payload: dict[str, Any]):
    """Persist a human decision only after a verified output has reached review."""
    decision = payload.get("decision")
    if not isinstance(decision, str) or decision not in {"approved", "changes_requested", "rejected"}:
        raise APIError(422, "Review decision must be approved, changes requested, or rejected.")
    notes = bounded_text(payload.get("notes"), "Review notes", 0, 2000, "")
    with connect() as db:
        job = db.execute("""SELECT j.status,j.output_path,j.commercial_use,j.rights_version,j.project_id,j.project_version,
                            a.permission_status,a.permission_expires_at,a.commercial_permission_status,
                            a.commercial_permission_expires_at,a.permission_version
                            FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.id=?""", (job_id,)).fetchone()
        if not job:
            raise APIError(404, "Job not found.")
        if job["status"] != "review" or not job["output_path"]:
            raise APIError(409, "Only a verified rendered output can enter human review.")
        if decision == "approved":
            if (not classify_permission(job["permission_status"], job["permission_expires_at"])
                    or job["rights_version"] != job["permission_version"]
                    or (job["commercial_use"] and not classify_permission(
                        job["commercial_permission_status"], job["commercial_permission_expires_at"]))):
                raise APIError(403, "Current rights no longer match this draft; review the source permissions and render again.")
            output = local_file("outputs", job["output_path"])
            if output.stat().st_size <= 0:
                raise APIError(409, "The verified draft file is missing or empty.")
        status = decision
        db.execute("INSERT INTO reviews(id,job_id,decision,notes,created_at) VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), job_id, status, notes, now_iso()))
        db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, now_iso(), job_id))
        if job["project_id"]:
            review_id, reviewed_at = str(uuid.uuid4()), now_iso()
            db.execute("INSERT INTO project_reviews(id,project_id,job_id,project_version,decision,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                       (review_id, job["project_id"], job_id, job["project_version"] or 1, status, notes, reviewed_at))
            db.execute("UPDATE edit_projects SET review_status=?,review_notes=?,updated_at=? WHERE id=?",
                       (status, notes, reviewed_at, job["project_id"]))
            event(db, "edit_project", job["project_id"], "project_review", status,
                  f"job_id={job_id}; version={job['project_version'] or 1}; {notes}")
        event(db, "job", job_id, "human_review", status, notes)
    return {"id": job_id, "status": status}


def history():
    """Return the newest bounded set of audit events."""
    with connect() as db:
        return [row_dict(row) for row in db.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 500")]


def local_file(kind: str, file_name: str) -> Path:
    if kind not in {"outputs", "thumbnails", "logs"} or Path(file_name).name != file_name:
        raise APIError(404, "File not found.")
    root = {"outputs": OUTPUT_ROOT, "thumbnails": THUMB_ROOT, "logs": LOG_ROOT}.get(kind)
    if root is None:
        raise APIError(404, "File not found.")
    path = (root / file_name).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise APIError(404, "File not found.")
    return path


def publishing():
    """Declare that this build has no publishing integration or upload capability."""
    return {"enabled": False, "message": "Publishing is disabled. No upload connector is configured."}


