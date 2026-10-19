"""Structured-output schemas for AI stages. Every AI response is validated against these models and
then checked deterministically (utterance IDs exist, speakers match, ranges are ordered)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoundaryOut(_Strict):
    kind: Literal["opening_update", "intro", "pitch", "qa", "feedback", "score", "award"]
    start_utterance: str = Field(description="ID (u###) of the first utterance in this part")
    end_utterance: str = Field(description="ID (u###) of the last utterance in this part")
    confidence: float = Field(ge=0, le=1)
    reason: str


class SegmentsOut(_Strict):
    boundaries: list[BoundaryOut]
    notes: list[str]


class QuestionPartOut(_Strict):
    label: str
    text: str


class QuestionOut(_Strict):
    utterance: str = Field(description="ID of the utterance where the question was asked")
    asker_role: Literal["investor", "moderator", "audience", "unknown"]
    paraphrase: str = Field(description="Short readable paraphrase; do not add facts")
    parts: list[QuestionPartOut]
    is_followup: bool
    answer_utterances: list[str]
    answered_summary: str = Field(description="What the founder actually answered, from the answer text only")
    unresolved: str = Field(description="What remained unanswered or unclear; empty if fully answered")
    completeness: Literal["complete", "incomplete", "gap"]
    importance: int = Field(ge=0, le=5)


class QuestionsOut(_Strict):
    questions: list[QuestionOut]


class FeedbackItemOut(_Strict):
    utterance: str | None
    speaker: str
    kind: Literal["positive", "recommendation", "comment", "nothing_to_add", "silent"]
    point: str = Field(description="One distinct point, paraphrased faithfully")
    theme: Literal["timeline_spend", "answer_length", "ip_competition", "investor_return", "slides", "regulatory",
                   "market", "traction", "team", "financials", "clarity", "other"]


class FeedbackOut(_Strict):
    items: list[FeedbackItemOut]


class PriorityOut(_Strict):
    title: str
    issue: str
    why_it_matters: str
    what_to_change: str
    better_version: str
    evidence_utterances: list[str]


class ReportProseOut(_Strict):
    assessment: str = Field(description="3-5 sentences, direct and specific")
    worked: list[str] = Field(description="2-3 specific observations grounded in panel comments")
    priorities: list[PriorityOut] = Field(description="Exactly the top three improvement priorities")


def json_schema(model: type[BaseModel]) -> dict:
    """JSON schema with additionalProperties: false everywhere (required by structured outputs)."""
    schema = model.model_json_schema()

    def fix(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                if "properties" in node:
                    node["required"] = list(node["properties"].keys())
            for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
                node.pop(k, None)
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    return schema
