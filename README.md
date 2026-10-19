# Variety Shorts Factory

A local-first Windows workstation for creator-supplied media. The working local pipeline provides import, SHA-256 duplicate detection, creator and permission records, timestamped vertical rendering with validated SRT captions, output verification, a persistent review queue, safe retry/recovery states, and an audit trail. It is not a clip downloader or publishing tool.

## Current implementation

- Standard-library threaded localhost API and a responsive, dependency-free browser interface.
- Versioned SQLite schema migration, foreign keys, interrupted-job recovery, and persistent workflow records.
- Streaming upload to a managed local source folder. Imported originals are never edited in place.
- Supported imports: MP4, MOV, WebM. Defaults: 2 GiB per file, four hours maximum source duration.
- Permission begins as **unverified**. Rendering requires a linked creator, a granted permission record, and an unexpired (or non-expiring) permission record.
- The editor streams the selected local source for playback and timestamp scrubbing, supports playhead in/out buttons, 0–100% horizontal/vertical portrait framing, three crop/zoom presets, editorial notes, and optional Unicode SRT captions. Crop settings are stored with each render job.
- FFprobe inspects source duration, dimensions, and audio presence. FFmpeg renders a 1080×1920 crop using the chosen framing and preset; FFprobe verifies the result before it enters review.
- Duplicate active render submissions return the existing job. Only explicitly safe failures may be retried, up to three times.
- Review decisions persist locally and cannot be overwritten; make a new render to submit a changed edit. There is no upload connector and no automatic publishing.
- State-changing requests enforce same-origin localhost use. The server binds to `127.0.0.1` and sets a restrictive Content Security Policy.

An AI editing assistant, model-generated edit decisions, Twitch/YouTube discovery, remote acquisition, thumbnail generation, automatic caption transcription, long-form moment detection, OAuth, and YouTube publishing are not implemented. Editing is currently user-directed with FFmpeg. A public URL does not grant media or reuse rights. No live APIs are called.

## Architecture

`app/main.py` contains the SQLite schema, media validation, workflow operations, and FFmpeg worker. `app/server.py` provides the standard-library threaded localhost HTTP server. `app/static/` contains the browser application. The worker runs one render at a time in-process. `start.bat` starts the server bound only to loopback. The backend has no third-party Python runtime dependencies. Data is outside the repository by default.

## Setup and launch

1. Install Python 3.11+ from [python.org](https://www.python.org/downloads/windows/). No Python packages are required.
2. Install FFmpeg and FFprobe from the official [FFmpeg download page](https://ffmpeg.org/download.html), which links to Windows builds. Extract a trusted build and set `VSF_FFMPEG` and `VSF_FFPROBE` to the executable paths, or add the trusted `bin` directory to your user PATH. The app does not download or execute an installer.
3. Double-click `start.bat`. The API listens on `http://127.0.0.1:8765`.
4. To stop, focus the startup window and press **Ctrl+C**.

Optional configuration can be set in the shell or in a project `.env` file: `VSF_DATA_DIR`, `VSF_PORT`, `VSF_MAX_UPLOAD_BYTES`, `VSF_MAX_SOURCE_SECONDS`, `VSF_FFMPEG`, and `VSF_FFPROBE`. Environment variables take precedence over `.env` values.

Default storage locations:

- Database and audit events: `%USERPROFILE%\Videos\VarietyShortsFactory\factory.sqlite3`
- Original imported copies: `%USERPROFILE%\Videos\VarietyShortsFactory\sources`
- Rendered versions: `%USERPROFILE%\Videos\VarietyShortsFactory\outputs`
- Thumbnails: `%USERPROFILE%\Videos\VarietyShortsFactory\thumbnails`

The first run creates these folders without replacing existing data. Back up the database and media folders together. Do not put private media or credentials in this Git repository.

## Workflow

1. Import a creator-supplied media file from **Clip library**. The app records its SHA-256 and marks permission unverified.
2. Add the creator in **Creator sources**. Use **Rights record** for each source to record evidence, status, and optional expiry. Only record a grant if you have actually reviewed the permission.
3. Select the inspected, permitted source in **Editor**, play and scrub the source, use **Set In/Out at playhead**, adjust the portrait frame, choose a crop preset, optionally load or paste a UTF-8 SRT file, and add editorial notes. SRT timestamps are relative to the selected clip and validated before rendering.
4. Render locally. The app fails the job if FFmpeg is unavailable and only queues output for review after FFprobe verifies 1080×1920 dimensions and duration.
5. Approve or request changes in **Review queue**. Approval is a local record only.

If FFprobe is missing, a file can be imported but its inspection status is `inspection_required`; rendering is blocked. Once configured, use the asset's **Inspect** action without reimporting. If FFmpeg is missing, rendering fails with a recorded, retryable dependency error. Install a trusted FFmpeg build and configure the executable paths.

## Tests

Run `python -m unittest discover -s tests -v` for the standard-library regression suite. Run `python tests/pilot_run.py` for a disposable TCP pilot of the actual localhost server, UI, import, seeking, permission, persistence, and missing-tool gates. The pilot uses synthetic bytes and temporary storage, not private or creator media. A mocked render exercises output verification and caption handoff. A real encode still requires FFmpeg/FFprobe and creator-authorized sample media; live platform integrations have not been implemented or tested.

## Security assumptions and limitations

- Intended for one user on the local Windows machine. It binds to IPv4 loopback and rejects unexpected Host headers and cross-origin state-changing requests. It has no account system, encryption-at-rest layer, or multi-user authorization.
- Files and uploaded metadata are treated as untrusted. Filenames are reduced to a supported basename; uploads stream to bounded temporary files; FFmpeg receives an argument array rather than a shell command. Browser output is protected by escaping, same-origin checks, and a restrictive Content Security Policy.
- Import is not a rights determination. Permission evidence is a user-supplied record and does not guarantee copyright compliance or monetization.
- The app does not download media, contact platform APIs, upload videos, or expose local files publicly.
- Use only creator-provided files or media acquired through a permitted route. Keep original media backups and verify the scope of the creator's grant.

## Troubleshooting

- **Browser cannot connect:** keep the startup window open and confirm it reports the local URL. Another process may already use port 8765; set `VSF_PORT` to a free local port.
- **Inspection required:** set `VSF_FFPROBE` to the full path of `ffprobe.exe`, restart, and select **Inspect** on the existing asset.
- **Render fails / missing dependency:** set `VSF_FFMPEG` to the full path of `ffmpeg.exe` and check the Production history page.
- **Disk/storage error:** check free space and access to the configured data directory. Files are not automatically deleted.
- **Permission gate:** confirm that the correct creator is linked and the record is granted and not expired or revoked.

## Verification status

The workspace and installed tools were inspected before implementation. No FFmpeg/FFprobe executable or Pytest installation was found. The unit suite and mocked render flow pass, but the real-media acceptance workflow cannot be completed until FFmpeg/FFprobe and authorized sample footage are available. Uploading/publishing is disabled by design.
