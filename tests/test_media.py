"""Real FFmpeg tests: a short synthetic video produces a valid clip with the expected streams and duration,
and adjusting clip boundaries marks outputs stale."""
import pytest

from pitchforce.media import ffmpeg
from pitchforce.models import Artifact, Clip, Event, SourceFile
from pitchforce.services import clips
from pitchforce.services.ingest import ingest_source
from pitchforce.services.regenerate import stale_artifacts

from .conftest import company

pytestmark = pytest.mark.media


def test_probe_and_render_short_clip(tmp_path):
    src = tmp_path / "in.mp4"
    ffmpeg.make_test_video(src, 20.0, size="160x90", rate=10)
    info = ffmpeg.probe(src)
    assert info.has_video and info.has_audio and abs(info.duration_s - 20.0) < 0.6
    out = tmp_path / "out.mp4"
    ffmpeg.render_clip(src, out, 4.0, 11.5)
    o = ffmpeg.probe(out)
    assert o.has_video and o.has_audio and abs(o.duration_s - 7.5) < 0.3


def test_probe_rejects_non_video(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"not a video at all" * 100)
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.probe(p)


def test_sample_clip_end_to_end_and_stale_after_boundary_change(fresh_sample):
    """Uses the sample transcripts with a short synthetic recording covering the Hemaflow session."""
    from pitchforce import sample_data
    from pitchforce.services.sources import register_file

    env, eid = fresh_sample
    db = env.session()
    ev = db.get(Event, eid)
    hema = company(db, eid, "Hemaflow Bio")
    # The full sample recording is ~42 minutes; render a cheap low-res copy of the right length.
    lines = sample_data.build_script()
    vid = env.dir / "rec.mp4"
    ffmpeg.make_test_video(vid, sample_data.recording_duration(lines), size="64x36", rate=2)
    src = register_file(db, ev, "video", "rec.mp4", vid)
    ingest_source(db, src)
    db.commit()
    clip = clips.update_candidate(db, ev, hema)
    db.commit()
    assert clip.status in ("ready_to_render", "boundary_candidate")
    am = db.query(clips.AlignmentMap).filter_by(video_source_id=src.id).one()
    assert am.method in ("identity_media", "anchor_match")
    # Expected media range from the synthetic script (pitch start -> last feedback utterance end)
    starts = [ln.t for ln in lines if ln.speaker == "Dana Okafor"]
    exp_start = sample_data.media_time(starts[0])
    assert abs(clip.start_media_s - exp_start) < 1.5
    dur = clip.end_media_s - clip.start_media_s
    assert 150 < dur < 400  # real content length (no padding to a 15-20 minute target)
    clip.status = "ready_to_render"
    art = clips.render(db, clip)
    db.commit()
    assert clip.status == "rendered", clip.validation
    assert clip.validation["ok"] and abs(clip.duration_s - dur) < 1.5
    checks = " ".join(clip.validation["checks"])
    assert "includes the start of the pitch" in checks and "end of the panel feedback" in checks
    assert art.kind == "clip_mp4" and art.size > 0
    # Re-render with identical boundaries does not create a duplicate artifact
    clips.render(db, clip)
    db.commit()
    assert db.query(Artifact).filter_by(company_id=hema.id, kind="clip_mp4").count() == 1
    # Package, then adjust boundaries -> outputs become stale
    from pitchforce.reports.package import build_company_package

    build_company_package(db, ev, hema)
    db.commit()
    assert not stale_artifacts(db, hema)
    clips.set_operator_boundaries(db, clip, clip.start_media_s - 5, clip.end_media_s, None)
    db.commit()
    stale = {a.kind for a in stale_artifacts(db, hema)}
    assert {"clip_mp4", "company_zip"} <= stale
    db.close()


_ = (Clip, SourceFile)
