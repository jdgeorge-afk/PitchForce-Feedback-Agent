"""Test fixtures. Every test environment uses its own temporary data directory and database.
Only synthetic data is used (pitchforce/sample_data.py and inline fixtures)."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("OUTBOUND_FETCH_ENABLED", "false")
os.environ.setdefault("AI_PROVIDER", "offline")
os.environ.setdefault("TRANSCRIPTION_PROVIDER", "none")
os.environ.setdefault("ENVIRONMENT", "test")


class Env:
    def __init__(self, name: str):
        self.dir = Path(tempfile.mkdtemp(prefix=f"pf_{name}_"))
        self._migrated = False

    def activate(self):
        from pitchforce import storage
        from pitchforce.config import reset_settings_cache
        from pitchforce.db import reset_engine

        os.environ["DATA_DIR"] = str(self.dir)
        reset_settings_cache()
        reset_engine()
        storage._storage = None
        if not self._migrated:
            from pitchforce.db import Base, get_engine
            from pitchforce import models  # noqa: F401

            Base.metadata.create_all(get_engine())
            self._migrated = True
        return self

    def session(self):
        from pitchforce.db import SessionLocal

        return SessionLocal()

    def cleanup(self):
        from pitchforce.db import reset_engine

        reset_engine()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def env():
    e = Env("t").activate()
    yield e
    e.cleanup()


@pytest.fixture
def db(env):
    s = env.session()
    yield s
    s.close()


_SAMPLE: dict = {}


@pytest.fixture
def sample(request):
    """A fully processed synthetic sample event (built once, re-activated for each test).
    Tests must not modify it; use ``fresh_sample`` for tests that change state."""
    if "env" not in _SAMPLE:
        e = Env("sample").activate()
        from pitchforce.jobs.worker import drain
        from pitchforce.services.sample import create_sample_event

        with e.session() as s:
            ev = create_sample_event(s, None, with_video=False)
            s.commit()
            _SAMPLE["event_id"] = ev.id
        drain()
        _SAMPLE["env"] = e
        request.session.config.add_cleanup(e.cleanup)
    _SAMPLE["env"].activate()
    return _SAMPLE["env"], _SAMPLE["event_id"]


@pytest.fixture
def fresh_sample(env):
    from pitchforce.jobs.worker import drain
    from pitchforce.services.sample import create_sample_event

    with env.session() as s:
        ev = create_sample_event(s, None, with_video=False)
        s.commit()
        eid = ev.id
    drain()
    return env, eid


def company(db, event_id, name):
    from pitchforce.models import Company

    return db.query(Company).filter_by(event_id=event_id, display_name=name).one()
