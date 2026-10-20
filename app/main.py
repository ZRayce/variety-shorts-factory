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
import threading
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


def initialize_database() -> None:
    """Create the versioned local schema without replacing existing data."""
    with connect() as db:
        schema_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if schema_table and DB_PATH.is_file():
            old_version = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if old_version and old_version[0] < 5:
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
                width INTEGER, height INTEGER, has_audio INTEGER, creator_id TEXT REFERENCES creators(id),
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
        """)
        db.execute("UPDATE schema_version SET version=5")


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


def validate_srt(source: str, clip_duration: float) -> str:
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
    if len(blocks) > 100:
        raise ValueError("A Short may contain at most 100 caption cues.")
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
    executable = shutil.which(FFPROBE) if not Path(FFPROBE).is_file() else FFPROBE
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=width,height,duration:format=duration", "-of", "json", str(path)],
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
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_SOURCE_SECONDS:
            return None
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            return None
        audio_result = subprocess.run([executable, "-v", "error", "-select_streams", "a:0", "-show_entries",
                                       "stream=codec_type", "-of", "json", str(path)],
                                      capture_output=True, text=True, timeout=45, check=True)
        audio_payload = json.loads(audio_result.stdout)
        audio_streams = audio_payload.get("streams") if isinstance(audio_payload, dict) else None
        return {"duration": duration, "width": width, "height": height,
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
    return {"status": "ok" if database_ok else "degraded", "ffmpeg_available": bool(shutil.which(FFMPEG) or Path(FFMPEG).is_file()),
            "ffprobe_available": bool(shutil.which(FFPROBE) or Path(FFPROBE).is_file()),
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
        rows = db.execute("""SELECT a.id,a.filename,a.sha256,a.size_bytes,a.duration,a.width,a.height,a.has_audio,
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
            ffprobe_executable = shutil.which(FFPROBE) if not Path(FFPROBE).is_file() else FFPROBE
            if ffprobe_executable and not metadata:
                remove_generated_file(target, "invalid_import_copy")
                raise APIError(422, "FFprobe could not validate this file as supported video media.")
            status = "inspected" if metadata else "inspection_required"
            with connect() as db:
                db.execute("""INSERT INTO assets(id,filename,relative_path,sha256,size_bytes,duration,width,height,has_audio,
                             status,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (asset_id, name, relative, digest.hexdigest(), size,
                            metadata["duration"] if metadata else None, metadata["width"] if metadata else None,
                            metadata["height"] if metadata else None, int(metadata["has_audio"]) if metadata else None,
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
    executable = shutil.which(FFPROBE) if not Path(FFPROBE).is_file() else FFPROBE
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
            db.execute("UPDATE assets SET duration=?,width=?,height=?,has_audio=?,status='inspected' WHERE id=?",
                       (metadata["duration"], metadata["width"], metadata["height"], int(metadata["has_audio"]), asset_id))
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


def queue_render(asset_id: str, payload: dict[str, Any]):
    """Validate rights and cut bounds, persist a job, and queue its single-worker render."""
    with JOB_QUEUE_LOCK:
        return _queue_render_locked(asset_id, payload)


def _queue_render_locked(asset_id: str, payload: dict[str, Any]):
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
    with connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise APIError(404, "Asset not found.")
        if not classify_permission(asset["permission_status"], asset["permission_expires_at"]):
            raise APIError(403, "A current creator permission record is required before rendering.")
        if commercial_use and not classify_permission(asset["commercial_permission_status"],
                                                      asset["commercial_permission_expires_at"]):
            raise APIError(403, "A separate current commercial-use permission record is required for this render.")
        if asset["duration"] is None:
            raise APIError(409, "Media duration is unavailable. Configure FFprobe and inspect this file before rendering.")
        raw_start, raw_end = payload.get("start_seconds"), payload.get("end_seconds")
        if (not isinstance(raw_start, (int, float)) or isinstance(raw_start, bool)
                or not isinstance(raw_end, (int, float)) or isinstance(raw_end, bool)):
            raise APIError(422, "Select valid numeric timestamps.")
        try:
            start, end = validate_cut(raw_start, raw_end, asset["duration"], profile["max_seconds"])
        except ValueError as exc:
            raise APIError(422, str(exc)) from exc
        try:
            captions_srt = validate_srt(payload.get("captions_srt", ""), end - start)
        except ValueError as exc:
            raise APIError(422, str(exc)) from exc
        duplicate = db.execute("""SELECT id FROM jobs WHERE asset_id=? AND status IN ('queued','running')
                                 AND start_seconds=? AND end_seconds=? AND preset=? AND editorial_notes=? AND captions_srt=?
                                 AND crop_x=? AND crop_y=? AND profile=? AND commercial_use=?
                                 ORDER BY created_at DESC LIMIT 1""",
                               (asset_id, start, end, preset, editorial_notes, captions_srt, crop_x, crop_y,
                                profile_name, int(commercial_use))).fetchone()
        if duplicate:
            return {"id": duplicate["id"], "status": "queued", "already_queued": True, "publishing_enabled": False}
        pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if pending >= MAX_PENDING_JOBS:
            raise APIError(429, f"The local queue is full ({MAX_PENDING_JOBS} pending jobs). Wait for a job to finish or cancel one.")
        job_id = str(uuid.uuid4())
        now = now_iso()
        manifest = {"asset_id": asset_id, "rights_version": asset["permission_version"], "profile": profile_name,
                    "canvas": {"width": profile["width"], "height": profile["height"]}, "start_seconds": start,
                    "end_seconds": end, "preset": preset, "crop_x": crop_x, "crop_y": crop_y,
                    "captions_sha256": hashlib.sha256(captions_srt.encode("utf-8")).hexdigest() if captions_srt else None,
                    "commercial_use": commercial_use}
        db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,editorial_notes,
                     captions_srt,crop_x,crop_y,profile,rights_version,commercial_use,edit_manifest,created_at,updated_at)
                     VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (job_id, asset_id, "queued", start, end, preset, editorial_notes, captions_srt, crop_x, crop_y,
                    profile_name, asset["permission_version"], int(commercial_use), json.dumps(manifest), now, now))
        event(db, "job", job_id, "render", "queued")
    start_render_worker(job_id)
    return {"id": job_id, "status": "queued", "publishing_enabled": False}


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
    executable = shutil.which(FFMPEG) if not Path(FFMPEG).is_file() else FFMPEG
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
    duration = job["end_seconds"] - job["start_seconds"]
    anchor_x = job["crop_x"] / 100
    anchor_y = job["crop_y"] / 100
    crop_filter = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                   f"crop={width}:{height}:x=(in_w-{width})*{anchor_x}:y=(in_h-{height})*{anchor_y},setsar=1")
    zoom = {"clean_vertical": None, "punchline": 0.88, "reaction": 0.94}[job["preset"]]
    if zoom is not None:
        crop_filter += f",crop=iw*{zoom}:ih*{zoom},scale={width}:{height},setsar=1"
    caption_path = OUTPUT_ROOT / f"{job_id}.srt"
    render_log = LOG_ROOT / f"{job_id}.log"
    if job["captions_srt"]:
        crop_filter += f",subtitles=filename='{caption_path.name}':force_style='FontName=Arial,FontSize=22,Outline=2,Shadow=1,Alignment=2,MarginV=110'"
    command = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(job["start_seconds"]),
               "-i", str(source), "-t", str(duration), "-map", "0:v:0"]
    if job["has_audio"]:
        command.extend(["-map", "0:a?"])
    command.extend(["-vf", crop_filter,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]
    )
    if job["has_audio"]:
        command.extend(["-c:a", "aac", "-b:a", "128k"])
    command.extend(["-movflags", "+faststart", str(temporary_output)])
    with connect() as db:
        db.execute("UPDATE jobs SET status='running',stage='render',updated_at=? WHERE id=?", (now_iso(), job_id))
    try:
        if job["captions_srt"]:
            caption_path.write_text(job["captions_srt"], encoding="utf-8", newline="\n")
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
                or metadata["duration"] > duration + 1 or (job["has_audio"] and not metadata["has_audio"])):
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
        checks = [
            {"name": "output_file_nonempty", "status": "PASS"},
            {"name": "video_stream_probe", "status": "PASS"},
            {"name": "profile_resolution", "status": "PASS"},
            {"name": "duration_within_cut", "status": "PASS"},
            {"name": "source_audio_preserved", "status": "PASS" if job["has_audio"] else "WARN",
             "detail": "Source contains no audio track." if not job["has_audio"] else ""},
            {"name": "caption_syntax", "status": "PASS" if job["captions_srt"] else "NOT_APPLICABLE"},
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
        job = db.execute("""SELECT j.status,j.output_path,j.commercial_use,j.rights_version,
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


