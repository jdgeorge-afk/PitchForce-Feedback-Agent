from datetime import timedelta

from pitchforce.db import utcnow
from pitchforce.jobs import queue
from pitchforce.jobs.queue import HANDLERS, PermanentJobError, enqueue, handler
from pitchforce.jobs.worker import drain, run_one
from pitchforce.models import Company, ProcessingJob, ReportSection

CALLS = []


@handler("test_flaky")
def _flaky(ctx):
    CALLS.append(ctx.attempt)
    if ctx.attempt < 2:
        raise RuntimeError("transient")
    return {"ok": True}


@handler("test_permanent")
def _perm(ctx):
    raise PermanentJobError("bad input")


@handler("test_progress")
def _progress(ctx):
    ctx.progress("Step one", 0.5)
    return {}


def test_idempotent_enqueue(db):
    a = enqueue(db, "test_progress", "k1")
    b = enqueue(db, "test_progress", "k1")
    assert a.id == b.id and db.query(ProcessingJob).count() == 1


def test_retry_with_backoff_then_success(db):
    CALLS.clear()
    j = enqueue(db, "test_flaky", "f1")
    jid = queue.claim_next("w1")
    assert jid == j.id
    assert run_one(jid, "w1") == "retry"
    db.expire_all()
    job = db.get(ProcessingJob, j.id)
    assert job.status == "queued" and job.run_after > utcnow()
    job.run_after = utcnow() - timedelta(seconds=1)
    db.commit()
    drain("w1")
    db.expire_all()
    assert db.get(ProcessingJob, j.id).status == "succeeded" and CALLS == [1, 2]


def test_permanent_error_fails_immediately(db):
    j = enqueue(db, "test_permanent", "p1")
    drain("w")
    db.expire_all()
    job = db.get(ProcessingJob, j.id)
    assert job.status == "failed" and job.attempts == 1 and "bad input" in job.error


def test_recovery_after_worker_crash_via_lease_expiry(db):
    j = enqueue(db, "test_progress", "r1")
    assert queue.claim_next("dead-worker") == j.id  # worker claims, then "crashes"
    assert queue.claim_next("w2") is None  # lease still valid
    job = db.get(ProcessingJob, j.id)
    job.lease_until = utcnow() - timedelta(seconds=1)
    db.commit()
    assert queue.claim_next("w2") == j.id  # reclaimed after restart
    run_one(j.id, "w2")
    queue.finish(j.id, "dead-worker", "failed", "late")  # the dead worker cannot overwrite the outcome
    db.expire_all()
    job = db.get(ProcessingJob, j.id)
    assert job.status == "succeeded" and job.attempts == 2


def test_cancellation(db):
    j = enqueue(db, "test_progress", "c1")
    queue.cancel(db, j)
    assert db.get(ProcessingJob, j.id).status == "cancelled"
    j2 = enqueue(db, "test_progress", "c2")
    queue.claim_next("w")
    job = db.get(ProcessingJob, j2.id)
    queue.cancel(db, job)
    assert run_one(j2.id, "w") == "cancelled"


def test_dependencies_wait(db):
    a = enqueue(db, "test_progress", "d1")
    b = enqueue(db, "test_progress", "d2", {"after": [a.id]})
    first = queue.claim_next("w")
    assert first == a.id and queue.claim_next("w") is None  # b waits for a
    run_one(a.id, "w")
    assert queue.claim_next("w") == b.id


def test_reprocessing_does_not_duplicate_outputs(fresh_sample):
    env, eid = fresh_sample
    db = env.session()
    before = db.query(ReportSection).count()
    from pitchforce.services.processing import queue_processing
    from pitchforce.models import Event

    queue_processing(db, db.get(Event, eid), None)
    db.commit()
    drain()
    db.expire_all()
    assert db.query(ReportSection).count() == before
    db.close()


def test_one_company_failure_does_not_block_others(fresh_sample, monkeypatch):
    env, eid = fresh_sample
    db = env.session()
    import pitchforce.services.regenerate as R

    real = R.process_company

    def boom(db_, event, company, **kw):
        if company.display_name == "Owlvision":
            raise RuntimeError("simulated failure")
        return real(db_, event, company, **kw)

    monkeypatch.setattr(R, "process_company", boom)
    for c in db.query(Company).filter_by(event_id=eid):
        enqueue(db, "process_company", c.id, {"company_id": c.id}, event_id=eid, company_id=c.id, rerun=True, max_attempts=1)
    db.commit()
    drain()
    db.expire_all()
    status = {c.display_name: c.status for c in db.query(Company).filter_by(event_id=eid)}
    assert status["Owlvision"] == "failed"
    assert all(v == "ready" for k, v in status.items() if k != "Owlvision")
    db.close()


_ = HANDLERS
