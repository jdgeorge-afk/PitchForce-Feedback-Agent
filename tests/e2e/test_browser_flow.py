"""Browser end-to-end test (Playwright, Chromium):
create event -> upload fixtures -> analyze -> resolve a mismatch -> review/edit report -> generate exports -> download package.

Runs a real web server and a real background worker on a temporary database with synthetic fixtures.
Run with:  python -m pytest tests/e2e -m e2e
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server():
    data = Path(tempfile.mkdtemp(prefix="pf_e2e_"))
    port = _free_port()
    env = {**os.environ, "DATA_DIR": str(data), "OUTBOUND_FETCH_ENABLED": "false", "AI_PROVIDER": "offline",
           "WORKER_POLL_SECONDS": "0.3", "BASE_URL": f"http://127.0.0.1:{port}", "PYTHONIOENCODING": "utf-8"}
    subprocess.run([sys.executable, "-m", "pitchforce", "migrate"], cwd=ROOT, env=env, check=True)
    log = open(data / "server.log", "w")
    web = subprocess.Popen([sys.executable, "-m", "pitchforce", "web", "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
    worker = subprocess.Popen([sys.executable, "-m", "pitchforce", "worker"], cwd=ROOT, env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/login", timeout=1).status_code in (200, 303, 307):
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    # Synthetic fixtures (no video: keeps the browser test fast; clips are covered by tests/test_media.py)
    from pitchforce.sample_data import generate

    files = generate(data / "fixtures", with_video=False)
    yield base, files, data
    web.terminate()
    worker.terminate()
    log.close()


def _wait_jobs_idle(page, base, event_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = page.request.get(f"{base}/api/events/{event_id}/jobs").json()
        if data["jobs"] and not data["active"]:
            return data
        time.sleep(1)
    raise AssertionError("background jobs did not finish")


def test_full_operator_flow(server, tmp_path):
    from playwright.sync_api import sync_playwright

    base, files, data = server
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        # First-run setup
        page.goto(base + "/")
        page.fill("#email", "operator@example.com")
        page.fill("#name", "Event Operator")
        page.fill("#password", "a-long-password-1")
        page.fill("#password2", "a-long-password-1")
        page.click("button[type=submit]")
        page.wait_for_url(base + "/")
        # Create event
        page.click("text=New event")
        page.fill("#name", "E2E Synthetic PitchForce")
        page.fill("#event_date", "2026-09-17")
        page.fill("#wid", "812 3456 7890")
        page.click("button:has-text('Create event')")
        page.wait_for_url("**/sources")
        event_id = page.url.split("/events/")[1].split("/")[0]
        # Upload fixtures through the chunked uploader
        for kind, key in [("company_roster", "company_roster"), ("judge_roster", "judge_roster"), ("transcript", "transcript_txt"),
                          ("transcript", "transcript_vtt"), ("scores", "scores"), ("interest", "interest_csv")]:
            page.select_option("#kind", kind)
            with page.expect_navigation(timeout=60000):
                page.set_input_files("[data-uploader] input[type=file]", str(files[key]))
        _wait_jobs_idle(page, base, event_id)
        page.goto(f"{base}/events/{event_id}/sources")
        assert page.locator("text=could not read").count() == 0
        # Run processing
        page.goto(f"{base}/events/{event_id}")
        page.click("button:has-text('Run processing')")
        _wait_jobs_idle(page, base, event_id)
        # Resolve the presenter/company mismatch in the review queue
        page.goto(f"{base}/events/{event_id}/review")
        card = page.locator(".card", has_text="Transcript presenter “Leo Park”")
        assert card.count() == 1
        card.locator("button[value=confirm]").click()
        page.wait_for_load_state()
        assert page.locator(".card", has_text="Transcript presenter “Leo Park”").count() == 0
        _wait_jobs_idle(page, base, event_id)
        # Review and edit a report section
        page.goto(f"{base}/events/{event_id}/results")
        page.click("text=Hemaflow Bio")
        assert page.locator("text=Announced total: 21 / 30").count() >= 1
        sec = page.locator("#sec-worked")
        sec.locator("summary").click()
        sec.locator("textarea").fill("- **Operator note:** The panel valued the clear problem statement.")
        sec.locator("button[value=save]").click()
        page.wait_for_load_state()
        assert page.locator("#sec-worked .badge:has-text('edited')").count() == 1
        # Generate exports and download the package
        page.goto(f"{base}/events/{event_id}/downloads")
        page.locator(".card", has_text="Hemaflow Bio").locator("button:has-text('package')").click()
        page.wait_for_load_state()
        _wait_jobs_idle(page, base, event_id)
        page.goto(f"{base}/events/{event_id}/downloads")
        link = page.locator(".card", has_text="Hemaflow Bio").locator("a", has_text="_PitchForce_Package.zip")
        with page.expect_download() as dl:
            link.click()
        path = tmp_path / "pkg.zip"
        dl.value.save_as(path)
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            assert any(n.endswith(".pdf") for n in names) and any(n.endswith(".xlsx") for n in names)
            docx = next(n for n in names if n.endswith(".docx"))
            from docx import Document

            text = "\n".join(p.text for p in Document(io.BytesIO(z.read(docx))).paragraphs)
            assert "Operator note:" in text  # the operator's edit is in the exported report
        page.screenshot(path=str(data / "e2e_downloads.png"), full_page=True)
        browser.close()
