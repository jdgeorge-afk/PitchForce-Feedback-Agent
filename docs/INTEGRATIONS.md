# External integrations and credentials

The core product works with **no external credentials**: uploads, offline analysis, review, clips (local FFmpeg), reports, workbooks and packages.

| Integration | Status | What it needs | What it receives |
|---|---|---|---|
| Anthropic Claude (AI analysis) | Implemented, **not configured**. Tested with mocked responses only; the live test is opt-in. | `AI_PROVIDER=anthropic`, `ANTHROPIC_API_KEY` (optionally `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT`) | Transcript excerpts for one company at a time, plus extracted evidence for report prose |
| Transcription (Whisper-compatible HTTP API) | Implemented, **not configured**. Chunk merging is unit-tested; no live provider call has been made. | `TRANSCRIPTION_PROVIDER=openai_compatible`, `TRANSCRIPTION_API_URL`, `TRANSCRIPTION_API_KEY`, `TRANSCRIPTION_MODEL` | Audio extracted from the recording, sent only when an operator presses *Transcribe* |
| EcoVG official calendar | Implemented. Retrieval was checked against the live events.ecovg.com page structure (listing plus each event's ICS file); tests use fixtures. | None. Outbound fetching is on by default (`OUTBOUND_FETCH_ENABLED`). | Plain GET requests to allow-listed EcoVG hosts |
| Zoom account | Not required. Recordings, transcripts, chat and poll exports are uploaded. | — | — |
| Outlook / email | Not implemented (extension point). Email drafts are text files. | — | — |
| S3-compatible object storage | Not implemented (interface documented). Local private storage is used. | — | — |

To verify a configured AI provider:

```bash
PITCHFORCE_LIVE_ANTHROPIC=1 .venv/Scripts/python -m pytest tests/test_ai_provider.py -m live
```
