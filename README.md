# ClipForge

ClipForge is a local-first Windows video drafting workbench. It imports creator-supplied files, tracks editing and commercial-use permissions separately, queues FFmpeg jobs, validates outputs, and requires a human review decision. It does not publish or upload videos.

The existing dependency-free browser interface is retained and served from a loopback-only Python server. It runs in the user's browser at `http://127.0.0.1:8765`; it is not a cloud service or a native desktop executable.

## Current capabilities

- Multi-file local import for MP4, MOV, WebM, MKV, and AVI, streamed to managed storage with SHA-256 duplicate detection. File extension is not treated as proof of valid video.
- Creator/source records and versioned rights records, including editing permission, separate commercial-use permission, evidence references, optional expiry, source URL, license reference, attribution, restrictions, and date checked.
- Rendering is blocked unless editing permission is current. Commercial-use jobs additionally require current commercial-use clearance. Approval rechecks the current rights-record version and draft file.
- YouTube links can be saved as references. Link handling extracts and stores a video ID only; it does not fetch metadata, download, scrape, inspect, or create local media. A reference remains **MEDIA REQUIRED** until the user imports an authorized local file and records its rights.
- Two fixed output profiles: vertical 1080×1920 with a 60-second limit and widescreen 1920×1080 bounded by source duration. Users can scrub, set in/out points, adjust crop anchors, choose one of three crop/zoom formulas, and add validated SRT captions.
- Persistent queued jobs that resume after restart, one FFmpeg process at a time, safe queued/running cancellation, bounded diagnostic logs, retry for safe dependency/interruption failures, a conservative free-space check, temporary render output, and output verification with FFprobe.
- Draft preview, quality report, render logs, explicit local approval confirmation, and retry/reject/request-changes states. Approval never publishes.
- Local health page reports the database, FFmpeg, FFprobe, publishing, and AI capability state.
- SQLite schema migrations create a pre-migration backup rather than replacing existing records. Originals are never edited in place.

## AI editing status

AI editing is **not implemented**. No local model, speech recognizer, scene analyzer, highlight ranker, or AI edit-decision planner is bundled or configured. The current editing formulas are deterministic FFmpeg transforms controlled by the user; ClipForge does not claim to understand footage or choose good moments. No model is downloaded automatically.

## YouTube link and rights rules

A YouTube link is a reference only. It cannot be selected as render media. Use an authorized video file that you already have, import it locally, and record the actual grant and restrictions. Finding a public video or saving its URL does not establish reuse rights. ClipForge stores evidence supplied by the user and does not independently verify ownership, monetization eligibility, or legal scope.

## Architecture

- `app/main.py`: local SQLite schema, validation, rights gates, import, queue, render process, quality report, and review functions.
- `app/server.py`: standard-library threaded WSGI server, loopback binding, API routing, same-origin protection, and streamed media previews.
- `app/static/`: semantic HTML, vanilla JavaScript, and responsive CSS.
- `tests/`: standard-library unit, API, workflow, mocked-render, and disposable TCP pilot checks.
- `samples/`: neutral SRT sample and an example rights ledger whose entries are explicitly unverified.

The backend has no third-party Python runtime dependencies. SQLite metadata and media are stored outside the repository by default. The legacy `VarietyShortsFactory` data directory is kept as the default so earlier local records are not silently orphaned.

## Windows setup and launch

1. Install Python 3.11 or newer.
2. Ensure `ffmpeg.exe` and `ffprobe.exe` are available on PATH. Verify in PowerShell:

   ```powershell
   ffmpeg -version
   ffprobe -version
   ```

   If not installed, use a trusted Windows build linked from [FFmpeg's official download page](https://ffmpeg.org/download.html). ClipForge does not install or update FFmpeg for you. You can instead set `VSF_FFMPEG` and `VSF_FFPROBE` to full executable paths in a local `.env` file.
3. Double-click [`START_CLIPFORGE.bat`](START_CLIPFORGE.bat). The older [`start.bat`](start.bat) launcher remains available.
4. Open `http://127.0.0.1:8765` in a browser. Keep the console window open. Press **Ctrl+C** in that window to stop ClipForge.

No `pip install` step is needed. `requirements.txt` is included to make the zero-dependency runtime explicit.

Optional settings are listed in [`.env.example`](.env.example). Copy it to `.env` to use them; `.env` is ignored by Git. Environment variables already set in Windows take precedence. The default data folder is `%USERPROFILE%\Videos\VarietyShortsFactory` and contains:

- `factory.sqlite3` and permission-history backups
- `sources\` imported copies
- `outputs\` verified drafts
- `logs\` FFmpeg diagnostics
- `thumbnails\` reserved for local preview assets

Back up the database and media folders together. Do not put personal media or API keys in this repository.

## First-run smoke walkthrough

This creates an original synthetic clip with FFmpeg; it does not use copyrighted footage. Run in PowerShell after verifying FFmpeg is installed:

```powershell
ffmpeg -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=44100 -t 12 -c:v libx264 -pix_fmt yuv420p -c:a aac "$env:TEMP\clipforge-synthetic.mp4"
```

Then:

1. Start ClipForge and import `%TEMP%\clipforge-synthetic.mp4` from **Clip library**.
2. Add a creator named `Local synthetic test`.
3. Open that asset's **Rights record**. Mark editing permission granted and use the evidence note `Synthetic test clip generated locally for this test`. Keep commercial permission unverified unless you specifically want to test that separate gate.
4. Select **Inspect** on the asset. Once FFprobe reports the video duration and dimensions, open **Editor**.
5. Render a 5–8 second `Shorts · Vertical` draft and wait for it to appear in the queue. A separate `Long-form · Widescreen` render can be queued from the same source.
6. Review the output inline, inspect its quality report and log, then approve it locally if the video looks right. Approval does not upload anything.
7. Delete the synthetic clip yourself when finished; ClipForge never automatically removes source media.

## Tests and pilot run

Run from the project root:

```powershell
python -m unittest discover -s tests -v
node --check app/static/app.js
python -m compileall -q app tests
python tests/pilot_run.py
```

The unit suite exercises URL parsing, rights and commercial gates, profile bounds, migrations and backups, queue transitions, queued cancellation, output verification, log handling, and render-failure cleanup. The pilot starts the real loopback HTTP server with temporary data and tests HTML/CSS/JavaScript delivery, health, upload, seeking, permission gates, persistence, and unavailable-media-tool behavior. Render calls are mocked in regression tests.

The current workstation had Python and Node but no FFmpeg or FFprobe on PATH. The TCP pilot passed; real probing, a real encode, subtitle-filter validation, and running-FFmpeg cancellation could not be exercised here. Once both executables are installed, the synthetic walkthrough above is the real-media smoke test. Do not interpret mocked render tests as evidence of a real encode.

## Security and data assumptions

- Designed for one user on one Windows machine. The HTTP server binds to `127.0.0.1`, checks Host and cross-origin state changes, and sends restrictive browser security headers. It has no login, encryption-at-rest, or multi-user authorization.
- All file, URL, caption, and JSON inputs are treated as untrusted. Uploads have a configured byte cap; source names are sanitized; managed paths are resolved under their expected folders; API SQL is parameterized; FFmpeg receives a fixed argument array with no shell execution.
- FFmpeg writes to a temporary file. Only a successful, nonempty output that FFprobe validates for profile dimensions, duration, and expected audio presence can enter review. Existing output names are preserved on collision.
- FFmpeg logs go to disk, not an unbounded in-memory capture buffer. The UI shows job status without inventing a progress percentage.
- No telemetry, analytics, background internet requests, platform API calls, downloads, publishing, or automatic deletion are implemented.
- Database migration backup is created next to the database as `factory.vN.backup.sqlite3`; retain it until the upgraded app has been checked.

## Known limitations and next work

- No AI, automatic transcription, scene detection, highlight selection, face-aware framing, timeline tracks, transitions, audio normalization, export presets beyond the two fixed canvases, or external SRT sidecar export.
- YouTube links are saved locally as references only; this build does not query the YouTube Data API or return video metadata.
- The queue processes one render at a time, but users queue jobs individually; there is no multi-profile batch wizard or queue ordering UI.
- Long-running FFmpeg cancellation is implemented using process termination and temporary-file cleanup, but it still needs an integration run on a machine with FFmpeg installed.
- Black-frame, frozen-frame, loudness, caption safe-area, and full source-codec QA checks are not implemented. A conservative free-space preflight is present but does not guarantee the encode will fit.
- Rights and job history export, user-selectable output folders, approved-folder copying, and encrypted backup are not implemented.

Highest-value follow-ups are: run and refine both real FFmpeg profile smoke tests on Windows; add a dedicated batch/queue management view with progress and ordering; then design an optional local AI analysis interface with explicit model installation consent and capability checks.
