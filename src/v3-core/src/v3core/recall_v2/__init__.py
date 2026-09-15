# -*- coding: utf-8 -*-
"""G6A Recall Engine V2 — contract layer (additive, separable).

This package defines the *contract* layer for the next-generation recall
engine.  Nothing in this module is imported from ``v3core/__init__.py``,
and the legacy ``recall_pool`` hot path is not routed through any of
these types.  Plain stdlib dataclasses — no I/O, no provider/PG/LLM
calls.  Python >= 3.10 compatible.
"""
from __future__ import annotations

from .contracts import (
    # Constants
    LANE_KEYWORD, LANE_VECTOR, LANE_TOPIC, LANE_QA, LANE_EXPLICIT,
    ALL_LANES,
    DEFAULT_QUERY_LIMIT, DEFAULT_INJECTION_MAX_CHARS, DEFAULT_RERANK_LIMIT,
    # Enums
    DropReasonCode, CandidateEventType,
    # Immutable contracts
    QueryContext, LanePlan, QueryPlan, CanonicalAlgorithmSnapshot, DropReason,
    # Mutable runtime
    RecallCandidate,
    # Public record dataclasses
    ProvenanceRecord, ScoreRecord, CandidateEvent,
    StageTransition, DropEvent,
    # Helpers
    safe_metadata,
    build_default_query_plan, build_effective_query_plan,
    canonical_algorithm_snapshot,
)
from .trace import (
    RecallTrace, recall_trace_from_plan,
    CandidateSnapshot, LaneSummary, InjectionSummary,
)

__all__ = [
    "LANE_KEYWORD", "LANE_VECTOR", "LANE_TOPIC", "LANE_QA", "LANE_EXPLICIT",
    "ALL_LANES",
    "DEFAULT_QUERY_LIMIT", "DEFAULT_INJECTION_MAX_CHARS", "DEFAULT_RERANK_LIMIT",
    "DropReasonCode", "CandidateEventType",
    "QueryContext", "LanePlan", "QueryPlan",
    "CanonicalAlgorithmSnapshot", "DropReason",
    "RecallCandidate", "RecallTrace",
    # Public record dataclasses
    "ProvenanceRecord", "ScoreRecord", "CandidateEvent",
    "StageTransition", "DropEvent",
    # Trace summaries
    "CandidateSnapshot", "LaneSummary", "InjectionSummary",
    # Helpers
    "safe_metadata",
    "build_default_query_plan", "build_effective_query_plan",
    "canonical_algorithm_snapshot",
    "recall_trace_from_plan",
]