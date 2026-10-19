"""HTTP-level tests: authentication, authorization, CSRF, chunked/resumable uploads."""
import re

import pytest
from fastapi.testclient import TestClient

from pitchforce.auth import create_user
from pitchforce.models import Artifact, Company, Event, SourceFile


@pytest.fixture
def client(env):
    from pitchforce.web.app import create_app

    return TestClient(create_app())


def _login(c, email, pw):
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"csrf_token": tok, "email": email, "password": pw})
    assert r.status_code == 200 and "Sign out" in r.text
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").text).group(1)


def test_requires_login_and_first_run_setup(client, db):
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
    assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"
    create_user(db, "a@example.com", "long-password-1", is_admin=True)
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/events/x/jobs").status_code == 401


def test_csrf_required(client, db):
    create_user(db, "a@example.com", "long-password-1", is_admin=True)
    _login(client, "a@example.com", "long-password-1")
    r = client.post("/events/new", data={"name": "X", "event_date": "2026-01-01"})
    assert r.status_code == 403


def test_cross_event_and_artifact_access_denied(client, db, env):
    create_user(db, "owner@example.com", "long-password-1")
    create_user(db, "other@example.com", "long-password-2")
    from pitchforce.models import User

    owner = db.query(User).filter_by(email="owner@example.com").one()
    ev = Event(name="Private", event_date=__import__("datetime").date(2026, 1, 1), created_by=owner.id)
    db.add(ev)
    db.flush()
    c = Company(event_id=ev.id, display_name="Acme")
    db.add(c)
    db.flush()
    from pitchforce.storage import get_storage

    key = get_storage().make_key("t", ".txt")
    get_storage().put_bytes(key, b"secret")
    a = Artifact(event_id=ev.id, company_id=c.id, kind="email_txt", filename="x.txt", storage_key=key)
    db.add(a)
    db.commit()
    _login(client, "other@example.com", "long-password-2")
    assert client.get(f"/events/{ev.id}").status_code == 404
    assert client.get(f"/events/{ev.id}/companies/{c.id}").status_code == 404
    assert client.get(f"/artifacts/{a.id}/download").status_code == 404
    client.cookies.clear()
    _login(client, "owner@example.com", "long-password-1")
    assert client.get(f"/artifacts/{a.id}/download").content == b"secret"


def test_chunked_resumable_upload_and_validation(client, db):
    create_user(db, "a@example.com", "long-password-1", is_admin=True)
    csrf = _login(client, "a@example.com", "long-password-1")
    h = {"X-CSRF-Token": csrf}
    ev = Event(name="E", event_date=__import__("datetime").date(2026, 1, 1))
    db.add(ev)
    db.commit()
    data = ("[Host] 10:00:00\nWelcome everyone to the event today.\n\n" * 50).encode()
    init = client.post(f"/api/events/{ev.id}/uploads", json={"kind": "transcript", "filename": "t.txt", "size": len(data)}, headers=h).json()
    uid = init["upload_id"]
    half = len(data) // 2
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data[:half], headers=h).json()["received"] == half
    # Simulate a dropped connection: the client asks again and resumes where it left off.
    again = client.post(f"/api/events/{ev.id}/uploads", json={"kind": "transcript", "filename": "t.txt", "size": len(data)}, headers=h).json()
    assert again["upload_id"] == uid and again["received"] == half
    bad = client.put(f"/api/uploads/{uid}?offset=0", content=b"x", headers=h)
    assert bad.status_code == 409 and bad.json()["received"] == half
    client.put(f"/api/uploads/{uid}?offset={half}", content=data[half:], headers=h)
    done = client.post(f"/api/uploads/{uid}/complete", headers=h).json()
    src = db.get(SourceFile, done["source_id"])
    assert src.size == len(data) and src.kind == "transcript"
    # Wrong type / disguised content is rejected
    r = client.post(f"/api/events/{ev.id}/uploads", json={"kind": "video", "filename": "x.exe", "size": 10}, headers=h)
    assert r.status_code == 400
    init2 = client.post(f"/api/events/{ev.id}/uploads", json={"kind": "video", "filename": "fake.mp4", "size": 16}, headers=h).json()
    r = client.put(f"/api/uploads/{init2['upload_id']}?offset=0", content=b"this is not mp4!", headers=h)
    assert r.status_code == 400


def test_sample_pages_render(client, db, env):
    create_user(db, "a@example.com", "long-password-1", is_admin=True)
    csrf = _login(client, "a@example.com", "long-password-1")
    from pitchforce.jobs.worker import drain
    from pitchforce.services.sample import create_sample_event

    ev = create_sample_event(db, None, with_video=False)
    db.commit()
    drain()
    pages = [f"/events/{ev.id}", f"/events/{ev.id}/sources", f"/events/{ev.id}/people", f"/events/{ev.id}/review",
             f"/events/{ev.id}/results", f"/events/{ev.id}/downloads", f"/events/{ev.id}/jobs", "/settings?section=calendar"]
    for c in db.query(Company).filter_by(event_id=ev.id):
        pages += [f"/events/{ev.id}/companies/{c.id}?tab={t}" for t in ("report", "questions", "feedback", "score", "interest", "video", "transcript")]
    for p in pages:
        r = client.get(p)
        assert r.status_code == 200, p
        assert "SAMPLE MODE" in r.text or p.startswith("/settings")
    _ = csrf
