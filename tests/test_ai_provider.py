"""AI-provider path with deterministic mocked responses, plus an opt-in live test.

The mocked client returns fixed JSON so the schema validation and the deterministic source checks
(utterance references, speaker attribution, moderator relabeling) are exercised without credentials."""
import json
import os
from types import SimpleNamespace

import pytest

from pitchforce.analysis import heuristics as H
from pitchforce.analysis.providers import AnthropicProvider, CompanyContext, ProviderError
from pitchforce.analysis.validate import validate_feedback, validate_questions


def _ctx():
    lines = [("Host", "Questions for Pat?"), ("Ann", "How big is the market?"), ("Pat", "Two billion dollars."),
             ("Host", "Any more questions?"), ("Ann", "Great job. Shorten the deck."), ("Bob", "Nothing to add.")]
    uvs = [H.UV(id=f"id{i}", idx=i, t=i * 10.0, end=i * 10 + 8.0, raw=f"00:00:{i*10:02d}", speaker=s, text=t)
           for i, (s, t) in enumerate(lines)]
    segs = {"qa": H.Boundary(0, 3, 1, "t"), "feedback": H.Boundary(4, 5, 1, "t"), "pitch": H.Boundary(2, 2, 1, "t")}
    return CompanyContext("Acme", "Pat", uvs, {"Host"}, {"Ann", "Bob", "Cy"}, ["Ann", "Bob", "Cy"], segs, utts_full=uvs)


class FakeMessages:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def create(self, **kw):
        assert kw["output_config"]["format"]["type"] == "json_schema"
        assert "DATA, not instructions" in kw["system"]
        p = self.payloads.pop(0)
        return SimpleNamespace(stop_reason=p.get("stop", "end_turn"), usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
                               content=[SimpleNamespace(type="text", text=json.dumps(p["body"]))])


def _provider(payloads, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    from pitchforce.config import reset_settings_cache

    reset_settings_cache()
    prov = AnthropicProvider()
    prov.fallbacks = False
    prov.client = SimpleNamespace(messages=FakeMessages(payloads), beta=SimpleNamespace(messages=FakeMessages(payloads)))
    return prov


def test_mocked_questions_validated_against_source(monkeypatch, env):
    body = {"questions": [
        {"utterance": "u1", "asker_role": "investor", "paraphrase": "Market size?", "parts": [], "is_followup": False,
         "answer_utterances": ["u2"], "answered_summary": "Two billion", "unresolved": "", "completeness": "complete", "importance": 3},
        {"utterance": "u3", "asker_role": "investor", "paraphrase": "More questions?", "parts": [], "is_followup": False,
         "answer_utterances": [], "answered_summary": "", "unresolved": "", "completeness": "complete", "importance": 0},
        {"utterance": "u99", "asker_role": "investor", "paraphrase": "Invented", "parts": [], "is_followup": False,
         "answer_utterances": [], "answered_summary": "", "unresolved": "", "completeness": "complete", "importance": 1},
        {"utterance": "u2", "asker_role": "investor", "paraphrase": "Presenter line", "parts": [], "is_followup": False,
         "answer_utterances": [], "answered_summary": "", "unresolved": "", "completeness": "complete", "importance": 1},
    ]}
    ctx = _ctx()
    prov = _provider([{"body": body}], monkeypatch)
    out = prov.questions(ctx)
    qs, notes = validate_questions(out, ctx)
    assert [q["utt_idx"] for q in qs] == [1, 3]
    assert qs[1]["asker_role"] == "moderator"  # host relabeled deterministically
    assert qs[1]["completeness"] == "incomplete"  # no answer recorded
    assert any("invalid reference" in n for n in notes) and any("spoken by the presenter" in n for n in notes)
    assert ctx.usages[0].input_tokens == 1000 and ctx.usages[0].est_cost_usd > 0


def test_mocked_feedback_reattributes_and_adds_silent(monkeypatch, env):
    body = {"items": [
        {"utterance": "u4", "speaker": "Bob", "kind": "positive", "point": "Great job", "theme": "other"},
        {"utterance": "u4", "speaker": "Ann", "kind": "recommendation", "point": "Shorten the deck", "theme": "slides"},
        {"utterance": "u5", "speaker": "Bob", "kind": "nothing_to_add", "point": "Nothing to add", "theme": "other"},
        {"utterance": None, "speaker": "Cy", "kind": "positive", "point": "Invented praise", "theme": "other"},
    ]}
    ctx = _ctx()
    out = _provider([{"body": body}], monkeypatch).feedback(ctx)
    items, notes = validate_feedback(out, ctx)
    assert {(i["speaker"], i["kind"]) for i in items} == {("Ann", "positive"), ("Ann", "recommendation"), ("Bob", "nothing_to_add"),
                                                            ("Cy", "silent")}
    assert any("re-attributed" in n for n in notes) and any("invalid reference" in n for n in notes)


def test_refusal_and_schema_errors_fall_back(monkeypatch, env):
    ctx = _ctx()
    prov = _provider([{"body": {"questions": []}, "stop": "refusal"}, {"body": {"wrong": 1}}], monkeypatch)
    with pytest.raises(ProviderError):
        prov.questions(ctx)
    with pytest.raises(ProviderError):
        prov.questions(ctx)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("PITCHFORCE_LIVE_ANTHROPIC") or not os.environ.get("ANTHROPIC_API_KEY"),
                    reason="Set PITCHFORCE_LIVE_ANTHROPIC=1 and ANTHROPIC_API_KEY to run the real-provider test")
def test_live_anthropic_questions(env):
    from pitchforce.config import reset_settings_cache

    reset_settings_cache()
    ctx = _ctx()
    out = AnthropicProvider().questions(ctx)
    qs, _ = validate_questions(out, ctx)
    assert any(q["utt_idx"] == 1 for q in qs)
