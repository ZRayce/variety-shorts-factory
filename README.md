# ClipForge

ClipForge is a local-first Windows video drafting workbench. It imports creator-supplied files, tracks editing and commercial-use permissions separately, queues FFmpeg jobs, validates outputs, and requires a human review decision. It does not publish or upload videos.

The existing dependency-free browser interface is retained and served from a loopback-only Python server. It runs in the user's browser at `http://127.0.0.1:8765`; it is not a cloud service or a native desktop executable.

## Current capabilities

- Multi-file local import for MP4, MOV, WebM, MKV, and AVI, streamed to managed storage with SHA-256 duplicate detection. File extension is not treated as proof of valid video.
- Creator/source records and versioned rights records, including editing permission, separate commercial-use permission, evidence references, optional expiry, source URL, license reference, attribution, restrictions, and date checked.
- Rendering is blocked unless editing permission is current. Commercial-use jobs additionally require current commercial-use clearance. Approval rechecks the current rights-record version and draft file.
- YouTube links can be saved as references. Link handling extracts and stores a video ID only; it does not fetch metadata, download, scrape, inspect, or create local media. A reference remains **MEDIA REQUIRED** until the user imports an authorized local file and records its rights.
- Two fixed output profiles: vertical 1080×1920 with a 60-second limit and widescreen 1920×1080 bounded by source duration. Users can scrub, set in/out points, adjust crop anchors, choose one of three crop/zoom formulas, and add validated SRT captions.
- Versioned, persistent JSON edit-decision projects store source metadata (including frame rate where FFprobe reports it), rights-record version, ordered timeline segments, transcript timings, captions, caption style, profile, crop, audio-preservation settings, references, render settings, and review state. Each save appends an immutable project snapshot.
- Multi-segment project renders use FFmpeg trim/concat with source-time seeking, preserve source audio by default, retain earlier outputs, and verify the combined duration, frame size, and audio stream before human review.
- Candidate discovery runs low-resolution 2 fps scene-change and optional silence analysis. It scores only transparent measurable factors, preserves context padding, and falls back to review windows when a filter is missing, times out, or exceeds its bounded diagnostics limit.
- Candidates remain **PENDING** until a person adjusts and accepts or rejects them. They show source preview, timing, score factors, available signals, and context warnings. Accepting creates a persistent editable project; selected accepted projects can be batch queued.
- Gaming, podcast/interview, educational/tutorial, cinematic/storytelling, creator-commentary, and custom discovery presets use different duration and context windows. These are practical discovery defaults, not semantic or AI understanding.
- Source transcript SRT can be loaded locally. Timed overlap can be reported; text is not semantically analyzed. Caption SRT supports optional wrapping, system-font selection/fallback, color, size, outline, shadow, placement, and separate Shorts/long-form presets.
- Persistent queued jobs that resume after restart, one FFmpeg process at a time, safe queued/running cancellation, bounded diagnostic logs, retry for safe dependency/interruption failures, a conservative free-space check, temporary render output, and output verification with FFprobe.
- Draft preview, quality report, render logs, explicit local approval confirmation, and retry/reject/request-changes states. Approval never publishes.
- Local health page reports the database, FFmpeg, FFprobe, publishing, and AI capability state.
- Settings diagnostics check configured FFmpeg/FFprobe paths, the supported `C:\ffmpeg\bin` install location, then PATH; `-version` checks run with timeouts and report executable, version, and failure status. A synthetic two-second video/audio render is verified with FFprobe in a unique temporary subdirectory and removed afterward.
- SQLite schema migrations create a pre-migration backup rather than replacing existing records. Originals are never edited in place.

## AI editing status

Semantic AI editing is **not implemented**. Candidate discovery is a deterministic signal-based heuristic using FFmpeg scene changes, optional silence boundaries, duration, supplied transcript timings, and visible scoring factors. It does not identify speech, understand scene meaning, rank highlights semantically, predict virality, or automatically approve edits. No cloud model is required, no model downloads automatically, and all candidate decisions remain human-controlled.

## YouTube link and rights rules

A YouTube link is a reference only. It cannot be selected as render media. Use an authorized video file that you already have, import it locally, and record the actual grant and restrictions. Finding a public video or saving its URL does not establish reuse rights. ClipForge stores evidence supplied by the user and does not independently verify ownership, monetization eligibility, or legal scope.

## Architecture

- `app/main.py`: local SQLite schema and migrations, versioned edit plans, rights gates, candidate analysis, persistent review, queue, multi-segment FFmpeg rendering, and quality checks.
- `app/server.py`: standard-library threaded WSGI server, loopback binding, API routing, same-origin protection, and streamed media previews.
- `app/static/`: semantic HTML, vanilla JavaScript, and responsive CSS.
- `tests/`: standard-library unit/API/workflow tests, a disposable localhost pilot, and `integration_ffmpeg.py` for real synthetic-media analysis and renders.
- `samples/`: neutral SRT sample and an example rights ledger whose entries are explicitly unverified.

The backend has no third-party Python runtime dependencies. SQLite metadata and media are stored outside the repository by default. The legacy `VarietyShortsFactory` data directory is kept as the default so earlier local records are not silently orphaned.

## Windows setup and launch

1. Install Python 3.11 or newer.
2. Install FFmpeg and FFprobe. They can be on PATH, or configured as full paths in `.env`; verify either setup:

   ```powershell
   ffmpeg -version
   ffprobe -version
   ```

   If not installed, use a trusted Windows build linked from [FFmpeg's official download page](https://ffmpeg.org/download.html). ClipForge does not install or update FFmpeg for you. For a build outside PATH, copy `.env.example` to `.env` and set `VSF_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe` and `VSF_FFPROBE=C:\ffmpeg\bin\ffprobe.exe` to your actual executable locations.
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
5. In **Editor**, choose a content discovery preset and select **Find candidate clips**. Review scene/silence reasons, warnings, and the source preview; manually adjust in/out points before accepting a candidate. Acceptance creates a project draft, not an approved output.
6. Load the project, add or reorder timeline segments, choose the Shorts or widescreen output and caption preset, and save. The project and version history persist in the local database.
7. Select **Save timeline and render** or accept several candidates and use **Render selected accepted projects**. Review each verified output and quality report. Human approval remains required and never uploads anything.
8. Delete the synthetic clip yourself when finished; ClipForge never automatically removes source media.

## Tests and pilot run

Run from the project root:

```powershell
python -m unittest discover -s tests -v
node --check app/static/app.js
python -m compileall -q app tests
python tests/pilot_run.py
$env:VSF_FFMPEG='C:\ffmpeg\bin\ffmpeg.exe'
$env:VSF_FFPROBE='C:\ffmpeg\bin\ffprobe.exe'
python tests/integration_ffmpeg.py
```

The unit/API/workflow suite covers schema migration, project versions, segment and candidate bounds, rights snapshots, transcript overlap, caption styles, queue recovery, collisions, failures, and manual project/output review. FFmpeg diagnostics tests mock absent executables, permission/timeout/process failures, successful verification, invalid output, and cleanup. The TCP pilot exercises the real local HTTP server. The integration script runs diagnostics and the synthetic test render with real FFmpeg/FFprobe, then creates separate synthetic color/audio footage, checks scene and silence analysis, handles paths with spaces, renders multi-segment Shorts with Unicode captions/audio and widescreen output, verifies timestamps/order and output metadata, and confirms explicit local approval. It deletes its temporary database, media, logs, and renders on exit. Run each command to see the exact results for your machine; mocked tests do not substitute for this real-media test.

## Security and data assumptions

- Designed for one user on one Windows machine. The HTTP server binds to `127.0.0.1`, checks Host and cross-origin state changes, and sends restrictive browser security headers. It has no login, encryption-at-rest, or multi-user authorization.
- All file, URL, caption, and JSON inputs are treated as untrusted. Uploads have a configured byte cap; source names are sanitized; managed paths are resolved under their expected folders; API SQL is parameterized; FFmpeg receives a fixed argument array with no shell execution.
- Analysis is CPU-friendly and bounded: video analysis scales to 320 pixels at 2 fps, each FFmpeg pass has a timeout and 4 MiB diagnostic cap, transcript imports are capped, and at most 50 candidates are generated per request.
- Timed transcript content stays in the local SQLite project document. Candidate scoring only checks timestamp overlap; it does not infer meaning from transcript words.
- FFmpeg writes to a temporary file. Only a successful, nonempty output that FFprobe validates for profile dimensions, duration, and expected audio presence can enter review. Existing output names are preserved on collision.
- FFmpeg logs go to disk, not an unbounded in-memory capture buffer. The UI shows job status without inventing a progress percentage.
- No telemetry, analytics, background internet requests, platform API calls, downloads, publishing, or automatic deletion are implemented.
- Database migration backup is created next to the database as `factory.vN.backup.sqlite3`; retain it until the upgraded app has been checked.

## Known limitations and next work

- No semantic AI, automatic transcription, speech detection, face/object tracking, scene understanding, automatic silence removal, virality prediction, transitions, audio normalization/mixing/ducking/fades, B-roll/overlay compositing, or word-level caption highlighting.
- Candidate presets alter discovery durations and context padding only. Their scores are review aids and do not measure editorial quality. Candidate boundary warnings use detected silence as a clue and cannot determine whether cutting a pause changes meaning.
- The current timeline editor supports ordered source segments from one asset per project. It does not yet support mixed-source timelines, drag-and-drop tracks, transitions, independent audio clips, or visual overlays.
- Caption fonts use installed system fonts; if the selected font is missing, the render falls back and records a warning. The application does not bundle fonts.
- YouTube links are saved locally as references only; this build does not query the YouTube Data API or return video metadata.
- The render queue processes one FFmpeg encode at a time. Candidate projects can be batch queued, but there is no queue ordering or per-job progress percentage.
- Transcription remains optional and user supplied; no model is installed or downloaded.
- Black-frame, frozen-frame, loudness, caption safe-area, and full source-codec QA checks are not implemented. A conservative free-space preflight is present but does not guarantee the encode will fit.
- Rights and job history export, user-selectable output folders, approved-folder copying, and encrypted backup are not implemented.

The next high-value follow-ups are multi-source timeline support with overlays/transitions, deeper nonsemantic output QA (black/frozen frame and loudness checks), and an optional local transcription connector with explicit model-install consent and resource controls.
