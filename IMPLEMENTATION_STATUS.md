# Implementation status

_Last updated: 2026-10-19 (initial build, branch `initial-build`)._

## Completed and tested
- **Foundation:**
  - FastAPI web app, SQLAlchemy models (40 tables) and an Alembic migration (`0001`)
  - Private storage; scrypt auth with first-run admin setup and per-event access; CSRF; security headers
  - Durable DB job queue with leases, retries, dependencies, cancellation and idempotency keys
  - A separate worker process
- **Intake:**
  - Resumable chunked uploads with validation (extension allowlist, magic bytes, size limits)
  - Source inventory, plus "what can be produced" explanations for incomplete input
- **Parsing:**
  - VTT/SRT/TXT transcripts (wall-clock and recording-relative; gaps kept)
  - Zoom chat scores; CSV/XLSX rosters and scores (long or wide layout)
  - Zoom multi-section poll CSVs and multi-sheet workbooks (multiline multi-select, blank columns, Unicode, formula cells)
- **Identity:**
  - Exact, confirmed-alias and fuzzy *proposals* only; event or global scope; rejected pairings respected
  - Related entities recorded as relationships, never merged; shared emails flagged, never merged
- **Scoring:** individual vs announced totals; the confirmed-absence rule (18/30 case); no imputation for missing data; invalid, out-of-range and duplicate scores flagged; fractional scores; ties preserved.
- **Interest:**
  - Event filtering by date and webinar ID
  - Launch-to-company evidence (the filename alone is never enough)
  - Amended and duplicate submissions handled; contradictions flagged; anonymous responses counted without invented contacts
  - Missing data distinguished from zero
- **Analysis (offline provider):**
  - Presenter, host and judge detection
  - Intro, opening update, pitch, Q&A, feedback, score and award boundaries
  - Questions (multi-part, follow-ups, moderator vs investor, incomplete/gap)
  - Feedback (positive, recommendation, nothing to add, silent)
  - Founder claims kept unverified; relationship cues
- **AI provider (Anthropic):** staged structured-output calls with deterministic validation and fallback to offline analysis. Tested with mocked responses only.
- **Media:**
  - FFmpeg probe and render (frame-accurate H.264/AAC)
  - Alignment (identity, multi-anchor matching with edit/pause pieces and drift, operator offset, manual)
  - Clip editor; validation of duration, streams, opening and closing, and that the next company is excluded; thumbnails; expiring and revocable share links
- **Review and editing:**
  - Review queue with typed resolutions and recorded decisions
  - Section editing, approval and regeneration that never overwrites edits
  - Stale tracking for sections, clips and packages
  - Transcript line reassignment and speaker overrides
- **Deliverables:**
  - Report PDF and DOCX; founder interest XLSX (formula-injection safe, typed dates, filters, frozen headers)
  - Email draft; evidence appendix; manifest; company ZIP
  - Event ZIP with audit workbook, issues summary and structured JSON
- **Official content:**
  - Calendar retrieval with ICS date verification, SSRF guard, cache and refresh, relevance matching (past and unverified sessions excluded)
  - Versioned, editable best practices with document import (duplicates, archived copies, contradiction flags)
  - Advisory invitation (no prices)
- **Sample mode:** synthetic event with video, clearly labeled everywhere.
- **Retention:** event deletion removes files; an hourly purge of archived events runs when `RETENTION_DAYS` is greater than 0.

## Current commands
- Run locally: `.venv/Scripts/python -m pitchforce dev` (web and worker), then open http://127.0.0.1:8000
- Production processes: `python -m pitchforce web` and `python -m pitchforce worker`
- Migrations: `python -m pitchforce migrate`
- Sample event from the command line: `python -m pitchforce sample`
- Tests: `.venv/Scripts/python -m pytest` (includes the Playwright e2e and real-FFmpeg tests)
- Regenerate the example outputs: `.venv/Scripts/python scripts/generate_examples.py`

## Tests run (this session)
- Unit, pipeline, export, web, media (real FFmpeg) and job tests: **all passing**. One live-provider test is skipped (no credentials).
- Browser e2e (Chromium): create event → upload fixtures → analyze → resolve mismatch → edit report → build package → download and verify: **passing**.
- Visual inspection:
  - The 3-page sample report PDF was rendered to images and checked.
  - The workbook contents were rendered and checked.
  - Screenshots were taken of the overview, results, review and company workspace (report and video tabs).

## Outstanding work
- Run the October 15 acceptance case on the **real** private files (docs/ACCEPTANCE.md).
- Live verification of the Anthropic and transcription providers once credentials exist.
- `docker compose` path written but **not run here** (Docker is not installed in the build environment). PostgreSQL support is written but not exercised; tests ran on SQLite.
- S3-compatible storage backend (interface only).
- Outlook draft integration (extension point only).
- Offline heuristics are tuned on synthetic transcripts. Expect to confirm more boundaries on real recordings. The AI provider is the intended path for real events.
- Accessibility: keyboard focus styles, labels and skip link are in place; no formal audit has been done.
- The original EcoVG cheat-sheet files have not been imported (they were not available); the defaults come from the brief.

## Genuine blockers
- None for local use. Real-data acceptance needs the private source files; AI and transcription need credentials.

## Next concrete steps
1. Import the real October 15 files into a local instance and compare against docs/ACCEPTANCE.md.
2. Upload the original best-practice DOCX/ZIP in Settings and approve a reviewed version.
3. Configure `ANTHROPIC_API_KEY`, run the live test, and compare AI and offline extractions on the real event.
4. Deploy with docker-compose behind HTTPS.
