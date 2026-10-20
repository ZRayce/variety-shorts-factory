"""Zero-dependency threaded localhost HTTP server for the factory UI and API."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from socketserver import ThreadingMixIn

from app import main as core

log = logging.getLogger("vsf.http")
SECURITY_HEADERS = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                    ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                    ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")]


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Handle UI requests while a media upload or render is running."""

    daemon_threads = True
    allow_reuse_address = True


class QuietRequestHandler(WSGIRequestHandler):
    """Avoid logging request query strings or uploaded filenames."""

    def log_message(self, format: str, *args: object) -> None:
        return


def response(start_response, status: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
    """Emit a bounded HTTP response with local-only browser security headers."""
    phrase = {200: "OK", 201: "Created", 202: "Accepted", 206: "Partial Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
              409: "Conflict", 413: "Payload Too Large", 422: "Unprocessable Entity", 500: "Internal Server Error",
              411: "Length Required", 416: "Range Not Satisfiable", 429: "Too Many Requests",
              503: "Service Unavailable"}.get(status, "Error")
    start_response(f"{status} {phrase}", [("Content-Type", content_type), ("Content-Length", str(len(body))),
                                           *SECURITY_HEADERS])
    return [body]


def json_response(start_response, status: int, payload: object):
    """Serialize a JSON response while rejecting non-finite numeric values."""
    return response(start_response, status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def api_body(environ) -> dict:
    """Read and validate a small JSON request body using WSGI's bounded input stream."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError as exc:
        raise core.APIError(400, "Invalid Content-Length header.") from exc
    if length < 0 or length > 256 * 1024:
        raise core.APIError(413, "JSON request exceeds the 256 KiB limit.")
    return core.json_object(environ["wsgi.input"].read(length))


def application(environ, start_response):
    """Dispatch same-origin localhost requests to the API or bundled static files."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    host_header = environ.get("HTTP_HOST", "")
    host = urlsplit("//" + host_header).hostname
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return json_response(start_response, 403, {"detail": "This service only accepts localhost requests."})
    origin = environ.get("HTTP_ORIGIN")
    if method not in {"GET", "HEAD", "OPTIONS"} and origin:
        origin_host = urlsplit(origin).hostname
        if origin_host != host or urlsplit(origin).netloc.lower() != host_header.lower():
            return json_response(start_response, 403, {"detail": "Cross-origin state changes are blocked."})

    try:
        if method in {"GET", "HEAD"} and path == "/":
            return static_file(start_response, core.APP_ROOT / "static" / "index.html", method)
        if method in {"GET", "HEAD"} and path.startswith("/static/"):
            relative = Path(unquote(path[len("/static/"):]))
            root = (core.APP_ROOT / "static").resolve()
            target = (root / relative).resolve()
            if root not in target.parents:
                raise core.APIError(404, "File not found.")
            return static_file(start_response, target, method)

        if method == "GET":
            if path == "/api/health": return json_response(start_response, 200, core.health())
            if path == "/api/creators": return json_response(start_response, 200, core.list_creators())
            if path == "/api/assets": return json_response(start_response, 200, core.list_assets())
            if path == "/api/youtube-candidates": return json_response(start_response, 200, core.list_youtube_candidates())
            if path == "/api/candidates":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                return json_response(start_response, 200, core.list_candidates(query.get("asset_id", [None])[0]))
            if path == "/api/projects":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                return json_response(start_response, 200, core.list_edit_projects(query.get("asset_id", [None])[0]))
            if path == "/api/jobs": return json_response(start_response, 200, core.list_jobs())
            if path == "/api/history": return json_response(start_response, 200, core.history())
            if path == "/api/publishing": return json_response(start_response, 200, core.publishing())
            parts = [unquote(piece) for piece in path.strip("/").split("/")]
            if len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "source":
                return static_file(start_response, core.local_source(parts[2]), method, environ.get("HTTP_RANGE"))
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                return json_response(start_response, 200, core.get_job(parts[2]))
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                return json_response(start_response, 200, core.get_edit_project(parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return json_response(start_response, 200, core.list_edit_project_versions(parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "reviews":
                return json_response(start_response, 200, core.list_edit_project_reviews(parts[2]))
            if path.startswith("/api/files/"):
                pieces = path.split("/")
                if len(pieces) != 5:
                    raise core.APIError(404, "File not found.")
                return static_file(start_response, core.local_file(pieces[3], unquote(pieces[4])), method,
                                   environ.get("HTTP_RANGE"))

        if method == "POST":
            if path == "/api/import":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                filename = query.get("filename", [""])[0]
                return json_response(start_response, 200, core.import_local_file(filename, environ["wsgi.input"], environ.get("CONTENT_LENGTH")))
            if path == "/api/diagnostics":
                return json_response(start_response, 200, core.diagnose_media_tools())
            if path == "/api/diagnostics/render-test":
                return json_response(start_response, 200, core.run_ffmpeg_render_test())
            parts = [unquote(piece) for piece in path.strip("/").split("/")]
            if len(parts) == 4 and parts[0:2] == ["api", "assets"] and parts[3] == "inspect":
                return json_response(start_response, 200, core.inspect_asset(parts[2]))
            if len(parts) == 4 and parts[0:2] == ["api", "assets"] and parts[3] == "discover":
                data = api_body(environ)
                return json_response(start_response, 201, core.discover_candidates(parts[2], data))
            if len(parts) == 4 and parts[0:2] == ["api", "assets"] and parts[3] == "projects":
                data = api_body(environ)
                return json_response(start_response, 201, core.create_edit_project(parts[2], data))
            if len(parts) == 4 and parts[0:2] == ["api", "jobs"] and parts[3] == "cancel":
                return json_response(start_response, 200, core.cancel_job(parts[2]))
            data = api_body(environ)
            if path == "/api/creators": return json_response(start_response, 200, core.create_creator(data))
            if path == "/api/youtube-candidates": return json_response(start_response, 201, core.add_youtube_candidate(data))
            if len(parts) == 4 and parts[0:2] == ["api", "assets"] and parts[3] == "permission":
                return json_response(start_response, 200, core.record_permission(parts[2], data))
            if len(parts) == 4 and parts[0:2] == ["api", "assets"] and parts[3] == "render":
                return json_response(start_response, 202, core.queue_render(parts[2], data))
            if len(parts) == 4 and parts[0:2] == ["api", "jobs"] and parts[3] == "review":
                return json_response(start_response, 200, core.review_job(parts[2], data))
            if len(parts) == 4 and parts[0:2] == ["api", "jobs"] and parts[3] == "retry":
                return json_response(start_response, 202, core.retry_job(parts[2]))
            if len(parts) == 4 and parts[0:2] == ["api", "candidates"] and parts[3] == "decision":
                data = api_body(environ)
                return json_response(start_response, 200, core.update_candidate(parts[2], data))
            if len(parts) == 4 and parts[0:2] == ["api", "projects"] and parts[3] == "render":
                return json_response(start_response, 202, core.queue_project_render(parts[2]))
        if method == "PUT":
            parts = [unquote(piece) for piece in path.strip("/").split("/")]
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                return json_response(start_response, 200, core.update_edit_project(parts[2], api_body(environ)))
        raise core.APIError(404, "Route not found.")
    except core.APIError as exc:
        return json_response(start_response, exc.status, {"detail": exc.detail})
    except Exception as exc:
        log.error("request failed method=%s path=%s category=%s", method, path[:120], type(exc).__name__)
        return json_response(start_response, 500, {"detail": "The local request failed. Check production history and server logs."})


def static_file(start_response, path: Path, method: str = "GET", range_header: str | None = None):
    """Stream a managed file, supporting one bounded byte range for responsive media seeking."""
    if not path.is_file():
        raise core.APIError(404, "File not found.")
    content_type = "text/plain; charset=utf-8" if path.suffix.lower() == ".log" else (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    extra_headers = [("Accept-Ranges", "bytes")]
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header[:100])
        if not match or size == 0 or not (match.group(1) or match.group(2)):
            start_response("416 Range Not Satisfiable", [("Content-Range", f"bytes */{size}"), ("Content-Length", "0"), *SECURITY_HEADERS])
            return [b""]
        first, last = match.groups()
        try:
            if not first:
                suffix_length = int(last)
                if suffix_length <= 0:
                    raise ValueError
                start = max(0, size - suffix_length)
                end = size - 1
            else:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
        except ValueError:
            start_response("416 Range Not Satisfiable", [("Content-Range", f"bytes */{size}"), ("Content-Length", "0"), *SECURITY_HEADERS])
            return [b""]
        if start >= size or end < start:
            start_response("416 Range Not Satisfiable", [("Content-Range", f"bytes */{size}"), ("Content-Length", "0"), *SECURITY_HEADERS])
            return [b""]
        status = 206
        extra_headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
    length = max(0, end - start + 1)
    phrase = "Partial Content" if status == 206 else "OK"
    start_response(f"{status} {phrase}", [("Content-Type", content_type), ("Content-Length", str(length)),
                                          *extra_headers, *SECURITY_HEADERS])
    if method == "HEAD":
        return [b""]
    return FileIterable(path, start, length)


class FileIterable:
    """Stream a local static/media byte range in bounded chunks and close its handle."""

    def __init__(self, path: Path, start: int, length: int):
        self.stream = path.open("rb")
        self.start = start
        self.remaining = length

    def __iter__(self):
        try:
            self.stream.seek(self.start)
            while self.remaining and (chunk := self.stream.read(min(1024 * 1024, self.remaining))):
                self.remaining -= len(chunk)
                yield chunk
        finally:
            self.stream.close()

    def close(self):
        self.stream.close()


def serve() -> None:
    """Start the private, threaded HTTP server on IPv4 loopback only."""
    port = int(os.environ.get("VSF_PORT", "8765"))
    with make_server("127.0.0.1", port, application, server_class=ThreadingWSGIServer,
                     handler_class=QuietRequestHandler) as server:
        core.recover_interrupted_jobs()
        core.resume_queued_jobs()
        print(f"ClipForge: http://127.0.0.1:{port}")
        print(f"Data directory: {core.DATA_ROOT}")
        print("Press Ctrl+C to stop. Publishing is disabled.")
        server.serve_forever()


if __name__ == "__main__":
    serve()
