"""Run a disposable TCP pilot of the actual localhost server and API."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def call(url: str, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None):
    """Make one localhost request and return its status, headers, and body."""
    request_headers = dict(headers or {})
    if body is not None:
        request_headers.setdefault("Content-Length", str(len(body)))
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=4) as response:
            return response.status, response.headers, response.read()
    except HTTPError as response:
        return response.code, response.headers, response.read()


def run_pilot() -> None:
    """Start a disposable server and exercise real HTTP, storage, and permission gates."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="vsf-pilot-", dir=ROOT / "tests") as data_dir:
        environment = os.environ.copy()
        environment.update({"VSF_DATA_DIR": data_dir, "VSF_PORT": str(port)})
        server = subprocess.Popen([sys.executable, "-m", "app.server"], cwd=ROOT, env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    output = server.stdout.read() if server.stdout else ""
                    raise RuntimeError(f"Server exited during startup: {output[-1200:]}")
                try:
                    status, _, health_body = call(f"{base}/api/health")
                    if status == 200:
                        break
                except (TimeoutError, URLError, OSError):
                    time.sleep(0.2)
            else:
                raise RuntimeError("Server did not become ready within 12 seconds.")

            page_status, _, page = call(base + "/")
            assert page_status == 200 and b"Variety Shorts Factory" in page
            style_status, _, style = call(base + "/static/style.css")
            script_status, _, script = call(base + "/static/app.js")
            assert style_status == 200 and b".review-video" in style and b"aspect-ratio:9/16" in style
            assert script_status == 200 and b"/api/assets/" in script and b"/source" in script
            health = json.loads(health_body)
            assert health["status"] == "ok" and health["publishing_enabled"] is False

            preview_bytes = b"synthetic preview output"
            preview_path = Path(data_dir) / "outputs" / "pilot-preview.mp4"
            preview_path.write_bytes(preview_bytes)
            preview_status, preview_headers, preview_body = call(
                f"{base}/api/files/outputs/pilot-preview.mp4", headers={"Range": "bytes=10-16"})
            assert preview_status == 206 and preview_body == b"preview"
            assert preview_headers.get("Content-Range") == f"bytes 10-16/{len(preview_bytes)}"
            assert preview_headers.get_content_type() == "video/mp4"

            filename = "pilot-synthetic.mp4"
            upload_status, _, upload_body = call(
                f"{base}/api/import?filename={quote(filename)}", "POST", b"synthetic pilot bytes",
                {"Content-Type": "application/octet-stream"})
            if health["ffprobe_available"]:
                assert upload_status == 422, f"FFprobe should reject synthetic bytes, got {upload_status}"
                print("PASS: server startup, browser UI, health endpoint, and invalid-media rejection")
                print("SKIP: authorized real-media import, edit, and render; no sample footage was supplied")
                return
            assert upload_status == 200, f"Expected safe import without FFprobe, got {upload_status}: {upload_body!r}"
            asset = json.loads(upload_body)
            assert asset["permission_status"] == "unverified"
            assert asset["status"] == "inspection_required"

            range_status, range_headers, range_body = call(
                f"{base}/api/assets/{asset['id']}/source", headers={"Range": "bytes=0-8"})
            assert range_status == 206 and range_body == b"synthetic"
            assert range_headers.get("Content-Range") == "bytes 0-8/21"

            creator_status, _, creator_body = call(
                base + "/api/creators", "POST", json.dumps({"name": "Pilot Creator"}).encode(),
                {"Content-Type": "application/json"})
            assert creator_status == 200, creator_body
            creator = json.loads(creator_body)
            permission_status, _, permission_body = call(
                f"{base}/api/assets/{asset['id']}/permission", "POST",
                json.dumps({"creator_id": creator["id"], "status": "granted",
                            "evidence": "Disposable pilot record"}).encode(),
                {"Content-Type": "application/json"})
            assert permission_status == 200, permission_body

            inspect_status, _, inspect_body = call(f"{base}/api/assets/{asset['id']}/inspect", "POST")
            assert inspect_status == 503, f"Missing FFprobe should be reported explicitly: {inspect_body!r}"
            render_status, _, render_body = call(
                f"{base}/api/assets/{asset['id']}/render", "POST",
                json.dumps({"start_seconds": 0, "end_seconds": 5}).encode(),
                {"Content-Type": "application/json"})
            assert render_status == 409, f"Uninspected media must not render: {render_body!r}"
            assets_status, _, assets_body = call(base + "/api/assets")
            assert assets_status == 200 and asset["id"].encode() in assets_body
            publishing_status, _, publishing_body = call(base + "/api/publishing")
            assert publishing_status == 200 and json.loads(publishing_body)["enabled"] is False

            print("PASS: real TCP server, browser UI, health, local import, source range seeking, creator/permission records, persistence, and safe missing-tool gates")
            print("SKIP: real inspection/render/review; FFprobe/FFmpeg and authorized sample footage are unavailable")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            if server.stdout:
                server.stdout.close()


if __name__ == "__main__":
    run_pilot()
