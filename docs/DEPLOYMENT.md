# Deployment

## Recommended shape
One small VM or container host with persistent disk, for example a 2–4 vCPU VM with 8 GB RAM and enough disk for recordings. It runs:
- `web` – `python -m pitchforce web --host 0.0.0.0 --port 8000`, behind an HTTPS reverse proxy (Caddy or nginx)
- `worker` – `python -m pitchforce worker` (one or more; FFmpeg runs here)
- PostgreSQL – managed or in a container
- Private storage – the `DATA_DIR` volume, backed up

Do not use a serverless or short-request platform for the worker: rendering a 15–20 minute clip and processing a 2-hour recording need long-running processes and local disk. `docker-compose.yml` implements this shape.

## Steps
1. Copy `.env.example` to `.env` and set:
   - `SECRET_KEY` (random), `ENVIRONMENT=production`, `COOKIE_SECURE=true`
   - `BASE_URL=https://your-host`
   - `ADMIN_EMAIL` / `ADMIN_PASSWORD`
2. Run `docker compose up -d --build`. Migrations run automatically on start.
3. Put the proxy in front of port 8000 and allow request bodies of at least `UPLOAD_CHUNK_MB` plus 1 MB (the default chunk is 8 MB).
4. Back up the PostgreSQL database and the `appdata` volume.

Without Docker: install Python 3.11+, FFmpeg and `pip install -r requirements-postgres.txt`, set `DATABASE_URL`, and run the web and worker commands under a process manager (systemd, NSSM on Windows).

## SQLite vs PostgreSQL
SQLite (the default) is fine for a single operator on one machine. SQLite allows one writer at a time; the app keeps transactions short and retries when the database is busy. For several operators or several workers, use PostgreSQL.

## Object storage
Files are stored privately under `DATA_DIR/storage` and served only through authorized routes. To use S3-compatible storage, implement the small interface in `pitchforce/storage.py` (`put_file`, `put_bytes`, `path`/`open`, `iter_range`, `delete`) with a local cache directory for FFmpeg. This is not implemented yet; see IMPLEMENTATION_STATUS.md.

## Security checklist
- [ ] HTTPS only; `COOKIE_SECURE=true`; a strong `SECRET_KEY`
- [ ] Administrator created; other users get access only to the events they need
- [ ] `RETENTION_DAYS` set to your policy; the worker deletes archived events (and their files) older than that, checking hourly
- [ ] AI and transcription providers configured only if your data policy allows sending transcripts and audio to them
- [ ] Backups encrypted; logs do not include contact lists (the app does not log them)
- [ ] Share links: `BASE_URL` must be reachable by founders for links to work; links expire after `SHARE_LINK_DAYS` and can be revoked
