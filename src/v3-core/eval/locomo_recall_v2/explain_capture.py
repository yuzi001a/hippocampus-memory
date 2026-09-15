"""G6C-B0 planner-evidence capture — evaluator-only helper.

The minimum reusable surface for one diagnostic: run
``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` against a
representative query, walk the entire plan tree, classify the
plan as ``seq_scan`` / ``ivfflat_index_scan`` / ``other``, and
return a sanitised, hashable record.

Classification rules (tree-walking — NOT just the top node):

  * If ANY node in the plan tree is an ``Index Scan`` whose
    ``Index Name`` contains ``ivfflat`` (case-insensitive),
    the report is ``CLASS_IVFFLAT_INDEX_SCAN``. This catches
    the canonical PG plan shape ``Limit -> Sort -> Index Scan``
    where the IVFFlat plan lives below a Sort or Limit parent.
  * Else if ANY node is a ``Seq Scan``, the report is
    ``CLASS_SEQ_SCAN``. A normal vector scan over an empty
    table still shows Seq Scan when the planner refuses to
    use the index.
  * Otherwise the report is ``CLASS_OTHER``.

The full plan tree is hashed (``plan_shape_sha256_16``) so an
auditor can confirm two runs produced the same plan shape
without reading the raw tree. Raw query vectors are NEVER
serialised — the supplied vector is hashed to
``query_vector_sha256_16`` (16 hex chars / 64 bits) only.

Production-only. Test fakes live in
``tests/test_explain_capture.py``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class ExplainCaptureError(RuntimeError):
    """Raised when the EXPLAIN output cannot be classified."""


# ---------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------


CLASS_SEQ_SCAN = "seq_scan"
CLASS_IVFFLAT_INDEX_SCAN = "ivfflat_index_scan"
CLASS_OTHER = "other"


@dataclass(frozen=True)
class ExplainPlanNode:
    """A sanitised, hashable plan-tree summary.

    ``any_ivfflat_index_scan`` and ``any_seq_scan`` are
    pre-computed tree-walking booleans so the auditor can
    confirm the classification without re-walking the tree.
    """

    top_node_type: str
    top_relation: str | None
    plan_rows: int | None
    plan_bytes: int | None
    classification: str
    any_ivfflat_index_scan: bool
    any_seq_scan: bool
    ivfflat_index_name: str | None
    # SHA-256 hex of the query vector (truncated to 16 chars).
    query_vector_sha256_16: str | None
    # Plan-shape fingerprint (sorted node-type list, hash of).
    plan_shape_sha256_16: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_node_type": str(self.top_node_type),
            "top_relation": (
                str(self.top_relation) if self.top_relation is not None else None
            ),
            "plan_rows": (
                int(self.plan_rows) if self.plan_rows is not None else None
            ),
            "plan_bytes": (
                int(self.plan_bytes) if self.plan_bytes is not None else None
            ),
            "classification": str(self.classification),
            "any_ivfflat_index_scan": bool(self.any_ivfflat_index_scan),
            "any_seq_scan": bool(self.any_seq_scan),
            "ivfflat_index_name": (
                str(self.ivfflat_index_name)
                if self.ivfflat_index_name is not None
                else None
            ),
            "query_vector_sha256_16": (
                str(self.query_vector_sha256_16)
                if self.query_vector_sha256_16 is not None
                else None
            ),
            "plan_shape_sha256_16": str(self.plan_shape_sha256_16),
        }


@dataclass(frozen=True)
class ExplainCaptureReport:
    """The full sanitised EXPLAIN output.

    ``raw_plan_shape`` lists every node's type / relation /
    index name so an auditor can replay the classification by
    hand without seeing the raw plan text. The vector SHA is
    included only when the caller supplied a query vector.
    """

    query_label: str
    top_node: ExplainPlanNode
    node_count: int
    raw_plan_shape: tuple[str, ...] = field(default_factory=tuple)
    explain_options: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_label": str(self.query_label),
            "top_node": self.top_node.to_dict(),
            "node_count": int(self.node_count),
            "raw_plan_shape": list(self.raw_plan_shape),
            "explain_options": list(self.explain_options),
        }


# ---------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------


def _walk_nodes(plan: Any) -> list[dict[str, Any]]:
    """Flatten a nested plan tree to a depth-first list of nodes."""

    out: list[dict[str, Any]] = []
    stack: list[Any] = [plan]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        out.append(node)
        for child in node.get("Plans") or ():
            stack.append(child)
    return out


def _classify_tree(
    nodes: list[dict[str, Any]],
) -> tuple[str, bool, bool, str | None]:
    """Walk the plan tree and return the canonical classification.

    Returns ``(classification, any_ivfflat, any_seq, ivfflat_index_name)``.
    The boolean flags are independent so a plan that uses BOTH a
    Seq Scan (for one table) and an IVFFlat Index Scan (for
    another) is correctly classified as IVFFlat — the IVFFlat
    branch wins because it proves the search protocol actually
    reached the planner.
    """

    any_ivfflat = False
    any_seq = False
    ivfflat_index_name: str | None = None
    for node in nodes:
        node_type = str(node.get("Node Type") or "").upper()
        if node_type == "INDEX SCAN":
            index_name = str(node.get("Index Name") or "")
            if "ivfflat" in index_name.lower():
                any_ivfflat = True
                if ivfflat_index_name is None:
                    ivfflat_index_name = index_name
        elif node_type == "SEQ SCAN":
            any_seq = True

    if any_ivfflat:
        return CLASS_IVFFLAT_INDEX_SCAN, any_ivfflat, any_seq, ivfflat_index_name
    if any_seq:
        return CLASS_SEQ_SCAN, any_ivfflat, any_seq, ivfflat_index_name
    return CLASS_OTHER, any_ivfflat, any_seq, ivfflat_index_name


def _vector_sha16(query_vector: Any) -> str | None:
    """SHA-256 hash of a query vector, truncated to 16 hex chars.

    Returns ``None`` when ``query_vector`` is ``None``. Never
    echoes the raw vector.
    """

    if query_vector is None:
        return None
    try:
        blob = json.dumps(
            [float(x) for x in query_vector],
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob).hexdigest()[:16]


def _plan_shape_sha16(nodes: list[dict[str, Any]]) -> str:
    """Stable hash of the textual plan shape (node types + relations)."""

    parts: list[str] = []
    for n in nodes:
        node_type = str(n.get("Node Type") or "")
        relation = str(n.get("Relation Name") or "")
        index_name = str(n.get("Index Name") or "")
        parts.append(f"{node_type}|{relation}|{index_name}")
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------


# Default EXPLAIN options — ``ANALYZE`` + ``BUFFERS`` so the
# reported plan carries actual run-time stats, not just planner
# estimates. ``FORMAT JSON`` keeps the parse path deterministic.
DEFAULT_EXPLAIN_OPTIONS: tuple[str, ...] = ("ANALYZE", "BUFFERS", "FORMAT JSON")


def capture_explain(
    conn: Any,
    *,
    sql: str,
    query_label: str,
    query_vector: Any = None,
    explain_options: tuple[str, ...] = DEFAULT_EXPLAIN_OPTIONS,
) -> ExplainCaptureReport:
    """Run ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <sql>`` and classify.

    The caller's ``sql`` is the **complete** ``SELECT`` —
    it must carry exactly one ``%s::vector`` placeholder
    when a vector is supplied.  ``EXPLAIN`` itself does
    NOT accept a ``USING`` clause (that is a server-side
    ``PREPARE`` / ``EXECUTE`` syntax, not ``EXPLAIN``
    syntax).  The helper therefore composes
    ``EXPLAIN (options) <sql>`` verbatim and forwards the
    vector as the **DB-API bound parameter** so the raw
    vector never touches the SQL string and is never
    persisted.

    Parameters
    ----------
    conn:
        A psycopg2-style connection. The helper opens exactly
        one cursor; the caller owns the connection lifecycle.
    sql:
        The exact ``SELECT`` whose plan should be captured.
        Must carry a single ``%s::vector`` placeholder when
        ``query_vector`` is supplied; ``EXPLAIN`` does not
        accept ``USING`` so the helper does NOT inject any
        extra clause.  The caller is responsible for the
        shape — the helper only binds the parameter.
    query_label:
        A short, non-secret identifier used as the report key.
    query_vector:
        Optional raw query vector; bound to the
        ``%s::vector`` placeholder in the caller's SQL.
        ``None`` means the SQL must contain no placeholder
        (e.g. a structural SQL canary with no vector
        argument).  The vector is hashed into
        ``query_vector_sha256_16`` only and never echoed in
        the issued SQL.
    explain_options:
        Override the EXPLAIN option list. Defaults to
        ``("ANALYZE", "BUFFERS", "FORMAT JSON")``.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise ExplainCaptureError("capture_explain: sql must be a non-empty string")
    if not isinstance(query_label, str) or not query_label.strip():
        raise ExplainCaptureError(
            "capture_explain: query_label must be a non-empty string"
        )

    options_clause = ", ".join(explain_options)
    # The caller owns the SQL shape — including its
    # ``%s::vector`` placeholder.  ``EXPLAIN`` itself does
    # NOT accept ``USING`` (that is a ``PREPARE`` /
    # ``EXECUTE`` syntax), so the helper composes the
    # EXPLAIN body verbatim as ``EXPLAIN (options) <sql>``
    # and binds the vector via the DB-API ``params`` tuple.
    explain_body = f"EXPLAIN ({options_clause}) {sql}"
    params: tuple[Any, ...] = (
        (query_vector,) if query_vector is not None else ()
    )

    try:
        cur = conn.cursor()
    except Exception as exc:
        raise ExplainCaptureError(
            f"capture_explain: conn.cursor() failed: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    try:
        cur.execute(explain_body, params)
    except Exception as exc:
        try:
            cur.close()
        except Exception:
            pass
        raise ExplainCaptureError(
            f"capture_explain: EXPLAIN execute failed: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    try:
        row = cur.fetchone()
    except Exception as exc:
        try:
            cur.close()
        except Exception:
            pass
        raise ExplainCaptureError(
            f"capture_explain: EXPLAIN fetchone failed: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass

    if row is None or len(row) < 1 or row[0] is None:
        raise ExplainCaptureError("capture_explain: EXPLAIN returned no rows")

    plan_payload = row[0]
    if isinstance(plan_payload, str):
        try:
            plan_payload = json.loads(plan_payload)
        except json.JSONDecodeError as exc:
            raise ExplainCaptureError(
                "capture_explain: EXPLAIN JSON unparseable"
            ) from exc
    if not isinstance(plan_payload, list) or not plan_payload:
        raise ExplainCaptureError("capture_explain: EXPLAIN shape unexpected")
    top = plan_payload[0]
    if not isinstance(top, dict):
        raise ExplainCaptureError("capture_explain: EXPLAIN top-level not a dict")
    plan_tree = top.get("Plan")
    if not isinstance(plan_tree, dict):
        raise ExplainCaptureError(
            "capture_explain: EXPLAIN top-level missing 'Plan'"
        )

    nodes = _walk_nodes(plan_tree)
    if not nodes:
        raise ExplainCaptureError("capture_explain: empty plan tree")

    top_node_dict = nodes[0]
    classification, any_ivfflat, any_seq, ivfflat_index_name = _classify_tree(nodes)
    top_node_type = str(top_node_dict.get("Node Type") or "")
    top_relation = top_node_dict.get("Relation Name")
    plan_rows = top_node_dict.get("Plan Rows")
    plan_bytes = top_node_dict.get("Plan Width")
    try:
        plan_rows_int = int(plan_rows) if plan_rows is not None else None
    except (TypeError, ValueError):
        plan_rows_int = None
    try:
        plan_bytes_int = int(plan_bytes) if plan_bytes is not None else None
    except (TypeError, ValueError):
        plan_bytes_int = None

    top_node = ExplainPlanNode(
        top_node_type=top_node_type,
        top_relation=(str(top_relation) if top_relation is not None else None),
        plan_rows=plan_rows_int,
        plan_bytes=plan_bytes_int,
        classification=classification,
        any_ivfflat_index_scan=any_ivfflat,
        any_seq_scan=any_seq,
        ivfflat_index_name=ivfflat_index_name,
        query_vector_sha256_16=_vector_sha16(query_vector),
        plan_shape_sha256_16=_plan_shape_sha16(nodes),
    )

    raw_shape = tuple(
        f"{n.get('Node Type', '?')}::{n.get('Relation Name', '')}"
        f"::{n.get('Index Name', '')}"
        for n in nodes
    )

    return ExplainCaptureReport(
        query_label=str(query_label),
        top_node=top_node,
        node_count=len(nodes),
        raw_plan_shape=raw_shape,
        explain_options=tuple(explain_options),
    )


__all__ = [
    "CLASS_IVFFLAT_INDEX_SCAN",
    "CLASS_OTHER",
    "CLASS_SEQ_SCAN",
    "DEFAULT_EXPLAIN_OPTIONS",
    "ExplainCaptureError",
    "ExplainCaptureReport",
    "ExplainPlanNode",
    "capture_explain",
]
