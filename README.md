# PitchForce Feedback

A private, browser-based application for EcoVG event operators. Upload a PitchForce event's materials, review anything uncertain, and download a feedback package for each presenting company:

- Founder feedback report (editable in the browser; PDF and Word)
- Investor/interest follow-up workbook (XLSX)
- Video clip of the session, from pitch through final feedback (MP4)
- Email draft (text only; nothing is sent automatically)
- Evidence appendix and a manifest of sources and limitations

Organizers also get an event package with an audit workbook, an issues summary and a structured JSON export.

The application keeps evidence and never invents scores, contacts, questions, commitments, event listings or video links. Anything it cannot settle goes to a review queue.

## Quick start (local, Windows/macOS/Linux)

Prerequisites: Python 3.11+ (tested on 3.13). FFmpeg is bundled through the `imageio-ffmpeg` package, so no separate install is needed.

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
```
(On macOS/Linux use `.venv/bin/python` instead.)

```bash
.venv/Scripts/python -m pitchforce dev
```

Open http://127.0.0.1:8000. On first visit, create the administrator account. Then choose **Load sample event** to explore every screen with clearly labeled synthetic data, or **New event** to start a real one.

The `dev` command runs migrations and starts the web server together with one background worker. In production, run them separately:

```bash
.venv/Scripts/python -m pitchforce web --host 0.0.0.0 --port 8000
```
```bash
.venv/Scripts/python -m pitchforce worker
```

Configuration lives in environment variables; see [.env.example](.env.example). With Docker, `docker compose up --build` runs PostgreSQL, the web server and the worker (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

## Tests

```bash
.venv/Scripts/python -m pytest
```

This runs the unit, pipeline, export, web, real-FFmpeg and Playwright browser tests. First install the browser once with `.venv/Scripts/python -m playwright install chromium`. The real-provider AI test is skipped unless `PITCHFORCE_LIVE_ANTHROPIC=1` and `ANTHROPIC_API_KEY` are set.

## Documentation

- [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) – plain-language guide for event operators
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – stack, components, data model, provenance, decisions
- [docs/SCORING.md](docs/SCORING.md) – judge scoring rules
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) – production deployment, storage, security checklist
- [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) – external services and which credentials are needed
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) – the October 15, 2026 acceptance case and how to run it on the real files
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) – what works, what was tested, what remains
- [examples/](examples/) – generated reports and workbooks from synthetic data

## Data handling

Real recordings, contacts and exports must never be committed. `data/`, `.env` and `private/` are git-ignored. All test fixtures are synthetic and use reserved `example.com`/`example.org` addresses.
