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
from urllib.parse import urlsplit


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
DB_PATH = DATA_ROOT / "factory.sqlite3"
MAX_UPLOAD_BYTES = int(os.environ.get("VSF_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_SOURCE_SECONDS = int(os.environ.get("VSF_MAX_SOURCE_SECONDS", "14400"))
MAX_SHORT_SECONDS = 60
RENDER_LOCK = threading.Lock()
JOB_QUEUE_LOCK = threading.Lock()
SOURCE_COMMIT_LOCK = threading.Lock()
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm"}
FFMPEG = os.environ.get("VSF_FFMPEG", "").strip() or "ffmpeg"
FFPROBE = os.environ.get("VSF_FFPROBE", "").strip() or "ffprobe"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vsf")

for directory in (SOURCE_ROOT, OUTPUT_ROOT, THUMB_ROOT):
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
        db.execute("UPDATE schema_version SET version=3")


initialize_database()


def recover_interrupted_jobs() -> None:
    """Mark jobs left queued or running by a prior process as safely retryable interruptions."""
    with connect() as db:
        interrupted = db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
        for row in interrupted:
            db.execute("""UPDATE jobs SET status='failed',stage='recovery',error_category='interrupted',
                         diagnostic='The application stopped before this job completed.',retry_safe=1,updated_at=? WHERE id=?""",
                       (now_iso(), row["id"]))
            event(db, "job", row["id"], "recovery", "interrupted", "Safe user-initiated retry is available.")


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
        raise ValueError("Choose an MP4, MOV, or WebM file.")
    stem = Path(name).stem
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).strip("._") or "media"
    return stem[:140] + Path(name).suffix


def validate_cut(start: float, end: float, duration: float) -> tuple[float, float]:
    """Validate a nonempty cut bounded by its inspected source and Shorts duration cap."""
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
               for value in (start, end, duration)):
        raise ValueError("Timestamps must be numbers.")
    if start < 0 or end <= start or end > duration or end - start > MAX_SHORT_SECONDS:
        raise ValueError("Select a valid segment of up to 60 seconds within the source duration.")
    return float(start), float(end)


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


def probe_media(path: Path) -> dict[str, Any] | None:
    """Inspect one file using FFprobe without loading media into memory."""
    executable = shutil.which(FFPROBE) if not Path(FFPROBE).is_file() else FFPROBE
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                capture_output=True, text=True, timeout=45, check=True)
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if video is None:
            return None
        duration = float(video.get("duration") or payload.get("format", {}).get("duration") or 0)
        if duration <= 0 or duration > MAX_SOURCE_SECONDS:
            return None
        return {"duration": duration, "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                "has_audio": any(stream.get("codec_type") == "audio" for stream in streams)}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
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
    return {"status": "ok", "ffmpeg_available": bool(shutil.which(FFMPEG) or Path(FFMPEG).is_file()),
            "ffprobe_available": bool(shutil.which(FFPROBE) or Path(FFPROBE).is_file()),
            "publishing_enabled": False, "data_dir": str(DATA_ROOT)}


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
                                  a.permission_expires_at,a.imported_at,c.name creator_name
                             FROM assets a LEFT JOIN creators c ON c.id=a.creator_id
                             ORDER BY a.imported_at DESC""").fetchall()
        assets = [row_dict(row) for row in rows]
        for asset in assets:
            asset["permission_valid"] = classify_permission(asset["permission_status"], asset["permission_expires_at"])
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
    if not isinstance(status, str) or status not in {"granted", "revoked", "rejected", "unverified"}:
        raise APIError(422, "Permission status must be granted, revoked, rejected, or unverified.")
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise APIError(422, "Permission expiry must be an ISO date/time string.")
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise APIError(422, "Permission expiry must be an ISO date/time string.") from exc
    with connect() as db:
        if not db.execute("SELECT id FROM creators WHERE id=?", (creator_id,)).fetchone():
            raise APIError(404, "Register the creator before recording permission.")
        changed = db.execute("""UPDATE assets SET creator_id=?,permission_status=?,permission_evidence=?,
                             permission_expires_at=? WHERE id=?""",
                             (creator_id, status, evidence, expires_at, asset_id)).rowcount
        if not changed:
            raise APIError(404, "Asset not found.")
        event(db, "asset", asset_id, "permission", status, evidence)
    return {"asset_id": asset_id, "permission_status": status}


def queue_render(asset_id: str, payload: dict[str, Any]):
    """Validate rights and cut bounds, persist a job, and queue its single-worker render."""
    with JOB_QUEUE_LOCK:
        return _queue_render_locked(asset_id, payload)


def _queue_render_locked(asset_id: str, payload: dict[str, Any]):
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
        if asset["duration"] is None:
            raise APIError(409, "Media duration is unavailable. Configure FFprobe and inspect this file before rendering.")
        raw_start, raw_end = payload.get("start_seconds"), payload.get("end_seconds")
        if (not isinstance(raw_start, (int, float)) or isinstance(raw_start, bool)
                or not isinstance(raw_end, (int, float)) or isinstance(raw_end, bool)):
            raise APIError(422, "Select valid numeric timestamps.")
        try:
            start, end = validate_cut(raw_start, raw_end, asset["duration"])
        except ValueError as exc:
            raise APIError(422, str(exc)) from exc
        try:
            captions_srt = validate_srt(payload.get("captions_srt", ""), end - start)
        except ValueError as exc:
            raise APIError(422, str(exc)) from exc
        duplicate = db.execute("""SELECT id FROM jobs WHERE asset_id=? AND status IN ('queued','running')
                                 AND start_seconds=? AND end_seconds=? AND preset=? AND editorial_notes=? AND captions_srt=?
                                 AND crop_x=? AND crop_y=?
                                 ORDER BY created_at DESC LIMIT 1""",
                               (asset_id, start, end, preset, editorial_notes, captions_srt, crop_x, crop_y)).fetchone()
        if duplicate:
            return {"id": duplicate["id"], "status": "queued", "already_queued": True, "publishing_enabled": False}
        job_id = str(uuid.uuid4())
        now = now_iso()
        db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,editorial_notes,
                     captions_srt,crop_x,crop_y,created_at,updated_at)
                     VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?,?)""",
                   (job_id, asset_id, "queued", start, end, preset, editorial_notes, captions_srt, crop_x, crop_y, now, now))
        event(db, "job", job_id, "render", "queued")
    start_render_worker(job_id)
    return {"id": job_id, "status": "queued", "publishing_enabled": False}


def retry_job(job_id: str):
    """Retry only a failed job explicitly marked safe, with current rights and a bounded retry count."""
    with connect() as db:
        job = db.execute("""SELECT j.*,a.permission_status,a.permission_expires_at,a.relative_path
                            FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.id=?""", (job_id,)).fetchone()
        if not job:
            raise APIError(404, "Job not found.")
        if job["status"] != "failed" or not job["retry_safe"] or job["retry_count"] >= 3:
            raise APIError(409, "This job is not eligible for another safe retry.")
        if not classify_permission(job["permission_status"], job["permission_expires_at"]):
            raise APIError(403, "A current creator permission record is required before retrying.")
        if not (SOURCE_ROOT / job["relative_path"]).is_file():
            raise APIError(409, "The original managed source file is missing.")
        changed = db.execute("""UPDATE jobs SET status='queued',stage='retry',error_category=NULL,diagnostic=NULL,
                             retry_safe=0,retry_count=retry_count+1,updated_at=? WHERE id=? AND status='failed'""",
                             (now_iso(), job_id)).rowcount
        if not changed:
            raise APIError(409, "The job changed while the retry was being queued.")
        event(db, "job", job_id, "retry", "queued", f"retry_count={job['retry_count'] + 1}")
    start_render_worker(job_id, retry=True)
    return {"id": job_id, "status": "queued", "retry_count": job["retry_count"] + 1}


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
        job = db.execute("SELECT j.*,a.relative_path,a.permission_status,a.permission_expires_at FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.id=?", (job_id,)).fetchone()
        if not job:
            return
        if not classify_permission(job["permission_status"], job["permission_expires_at"]):
            db.execute("UPDATE jobs SET status='failed',stage='permission_gate',error_category='permission',updated_at=? WHERE id=?", (now_iso(), job_id))
            event(db, "job", job_id, "permission_gate", "failed", "Permission missing, expired, or revoked.")
            return
        source = SOURCE_ROOT / job["relative_path"]
    executable = shutil.which(FFMPEG) if not Path(FFMPEG).is_file() else FFMPEG
    if not executable:
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',stage='render',error_category='missing_dependency',diagnostic='FFmpeg was not found.',retry_safe=1,updated_at=? WHERE id=?", (now_iso(), job_id))
            event(db, "job", job_id, "render", "failed", "FFmpeg missing")
        return
    output = OUTPUT_ROOT / f"{job_id}.mp4"
    duration = job["end_seconds"] - job["start_seconds"]
    anchor_x = job["crop_x"] / 100
    anchor_y = job["crop_y"] / 100
    crop_filter = (f"scale=1080:1920:force_original_aspect_ratio=increase,"
                   f"crop=1080:1920:x=(in_w-1080)*{anchor_x}:y=(in_h-1920)*{anchor_y}")
    zoom = {"clean_vertical": None, "punchline": 0.88, "reaction": 0.94}[job["preset"]]
    if zoom is not None:
        crop_filter += f",crop=iw*{zoom}:ih*{zoom},scale=1080:1920"
    caption_path = OUTPUT_ROOT / f"{job_id}.srt"
    if job["captions_srt"]:
        crop_filter += f",subtitles=filename='{caption_path.name}':force_style='FontName=Arial,FontSize=22,Outline=2,Shadow=1,Alignment=2,MarginV=110'"
    command = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(job["start_seconds"]),
               "-i", str(source), "-t", str(duration), "-vf", crop_filter,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)]
    with connect() as db:
        db.execute("UPDATE jobs SET status='running',stage='render',updated_at=? WHERE id=?", (now_iso(), job_id))
    try:
        if job["captions_srt"]:
            caption_path.write_text(job["captions_srt"], encoding="utf-8", newline="\n")
        subprocess.run(command, cwd=OUTPUT_ROOT, capture_output=True, text=True,
                       timeout=max(600, duration * 20), check=True)
        metadata = probe_output(output)
        if not metadata or metadata["width"] != 1080 or metadata["height"] != 1920 or metadata["duration"] > duration + 1:
            raise RuntimeError("Output verification failed.")
        report = {"valid_video": True, "width": metadata["width"], "height": metadata["height"],
                  "duration": metadata["duration"], "has_audio": metadata["has_audio"],
                  "silent_audio_allowed": not metadata["has_audio"], "warnings": []}
        with connect() as db:
            db.execute("UPDATE jobs SET status='review',stage='quality_assurance',output_path=?,quality_report=?,updated_at=? WHERE id=?",
                       (output.name, json.dumps(report), now_iso(), job_id))
            event(db, "job", job_id, "quality_assurance", "passed", json.dumps(report))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        remove_generated_file(output, "failed_render_output")
        diagnostic = exc.stderr[-500:] if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)[:500]
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',stage='render',error_category='render_failure',diagnostic=?,retry_safe=0,updated_at=? WHERE id=?",
                       (diagnostic, now_iso(), job_id))
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
        job = db.execute("SELECT status,output_path FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise APIError(404, "Job not found.")
        if job["status"] != "review" or not job["output_path"]:
            raise APIError(409, "Only a verified rendered output can enter human review.")
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
    if kind not in {"outputs", "thumbnails"} or Path(file_name).name != file_name:
        raise APIError(404, "File not found.")
    root = OUTPUT_ROOT if kind == "outputs" else THUMB_ROOT
    path = (root / file_name).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise APIError(404, "File not found.")
    return path


def publishing():
    """Declare that this build has no publishing integration or upload capability."""
    return {"enabled": False, "message": "Publishing is disabled. No upload connector is configured."}


