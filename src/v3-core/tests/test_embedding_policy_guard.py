"""Guard: every embedding call site must consciously choose its latency budget.

WHY THIS TEST EXISTS
--------------------
The production hole was created by *omission*: `call_embedding` defaulted to
`timeout=3, retries=0` (correct for a query inside the 8s realtime path, wrong for a
durable write), and a durable write that simply forgot to pass parameters silently
inherited it. The provider's p95 is ~0.58s but its tail exceeds 3s, so a forgotten
parameter meant a permanently lost embedding.

A named-policy API only helps if omission is *detectable*. This test is that
detector: it walks every `call_embedding` call site in the package and requires each
one to be either

  (a) explicitly budgeted — passes ``policy=`` (or explicit timeout/retries), or
  (b) listed in ``REALTIME_BY_DESIGN`` below with a reason.

Anything else fails, which forces a human to classify the new call site instead of
letting it inherit a default by accident. Adding a durable write without a policy is
therefore a test failure, not a production incident six weeks later.

`embed_batch` is exempt: it hardcodes `timeout=60` and takes `retries` as an explicit
parameter, so it has no inherited-default trap.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent / "src" / "v3core"

#: Call sites that are ALLOWED to omit an explicit budget, each with the reason.
#: Every entry is a realtime query embedding — i.e. it sits inside the user-facing 8s
#: budget, where the 3s/0 default is the intended behaviour and where a durable-write
#: retry would be actively harmful.
REALTIME_BY_DESIGN: dict[str, str] = {
    "__init__.py": "recall / prefetch / search_cards query embedding (8s budget)",
    "recall_pool.py": "yin/QA recall query embedding (8s budget)",
    "topic_recall.py": "topic match / chain_recall query embedding (8s budget)",
    "topic_refine.py": "TopicRefiner.match query embedding (8s budget)",
    "observer.py": "_recall_candidates query embedding (2.5s, 8s budget)",
    "embed_failures.py": "the policy-aware wrapper itself — it passes policy through",
    "embedding.py": "the definitions themselves",
}


def _iter_call_embedding_calls():
    """Yield (relpath, lineno, has_explicit_budget) for every call_embedding call."""
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        rel = path.relative_to(PKG).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name != "call_embedding":
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            budgeted = bool(
                kwargs & {"policy", "timeout", "retries"}
            )
            yield rel, node.lineno, budgeted


def test_every_call_embedding_site_chooses_a_budget():
    unbudgeted = [
        (rel, ln) for rel, ln, budgeted in _iter_call_embedding_calls() if not budgeted
    ]
    offenders = [
        (rel, ln) for rel, ln in unbudgeted
        if pathlib.PurePosixPath(rel).name not in REALTIME_BY_DESIGN
    ]
    assert not offenders, (
        "these call_embedding sites pass no policy and no explicit budget, so they "
        "silently inherit the realtime 3s/0 default:\n"
        + "\n".join(f"  {rel}:{ln}" for rel, ln in offenders)
        + "\n\nEither pass policy=DURABLE_WRITE_EMBED_POLICY (or another named policy), "
          "or add the file to REALTIME_BY_DESIGN with the reason it really is inside the "
          "8s budget."
    )


def test_durable_writes_do_not_use_the_realtime_policy():
    """A durable write must never select the realtime policy.

    The mirror image of the test above: it is not enough for a durable site to name
    *a* policy — naming the realtime one would reintroduce the same bug with a more
    confident-looking call site.
    """
    durable_files = {
        "ingest.py", "card_store.py", "yin_pool.py", "session_summary.py",
        "topic_store.py", "topic_create.py", "topic_edit.py", "topic_refine.py",
        "embed_topics.py", "import_.py", "active_memory_store.py",
    }
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name not in durable_files:
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name not in ("call_embedding", "embed_for_write"):
                continue
            for kw in node.keywords:
                if kw.arg == "policy" and isinstance(kw.value, ast.Name):
                    if kw.value.id == "REALTIME_EMBED_POLICY":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "durable-write call sites selecting the realtime policy:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_policy_objects_are_ordered_sensibly():
    """Realtime must stay short; durable/batch must actually be longer.

    Guards against a future edit that "simplifies" the policies by making them equal —
    which would silently delete the entire point of the hotfix.
    """
    from v3core.embedding import (
        REALTIME_EMBED_POLICY,
        DURABLE_WRITE_EMBED_POLICY,
        BATCH_EMBED_POLICY,
    )

    assert REALTIME_EMBED_POLICY.retries == 0, "the 8s path must not retry"
    assert REALTIME_EMBED_POLICY.timeout <= 3.0
    for durable in (DURABLE_WRITE_EMBED_POLICY, BATCH_EMBED_POLICY):
        assert durable.timeout > REALTIME_EMBED_POLICY.timeout
        assert durable.retries > REALTIME_EMBED_POLICY.retries


def test_realtime_allowlist_entries_actually_exist():
    """Keep the allowlist honest — a stale entry would hide a real file rename."""
    names = {p.name for p in PKG.rglob("*.py")}
    missing = [f for f in REALTIME_BY_DESIGN if f not in names]
    assert not missing, f"REALTIME_BY_DESIGN references files that no longer exist: {missing}"


def test_stream_primary_policy_is_shaped_5s_0():
    """The conversation_stream live-writer primary is a named 5s/0 single shot.

    Deploy plan §3–§4: the primary must cover the observed 3.488s provider tail
    while a 10s/2 inline chain would head-of-line-block the single-threaded
    serial writer (~33s worst case per item vs ~13 rows/min peak arrival).
    """
    from v3core.embedding import (
        STREAM_PRIMARY_EMBED_POLICY,
        DURABLE_WRITE_EMBED_POLICY,
        REALTIME_EMBED_POLICY,
    )

    assert STREAM_PRIMARY_EMBED_POLICY.name == "stream_primary"
    assert STREAM_PRIMARY_EMBED_POLICY.timeout == 5.0
    assert STREAM_PRIMARY_EMBED_POLICY.retries == 0
    assert STREAM_PRIMARY_EMBED_POLICY.timeout > REALTIME_EMBED_POLICY.timeout
    assert STREAM_PRIMARY_EMBED_POLICY.timeout < DURABLE_WRITE_EMBED_POLICY.timeout


def test_durable_write_policy_stays_10s_2():
    """Deferred/backfill keeps the patient budget — the split only moved the live writer."""
    from v3core.embedding import DURABLE_WRITE_EMBED_POLICY

    assert DURABLE_WRITE_EMBED_POLICY.name == "durable_write"
    assert DURABLE_WRITE_EMBED_POLICY.timeout == 10.0
    assert DURABLE_WRITE_EMBED_POLICY.retries == 2


def _policy_name_of(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "policy" and isinstance(kw.value, ast.Name):
            return kw.value.id
    return None


def _const_of(call: ast.Call, key: str):
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def test_livebuffer_source_write_uses_stream_primary_policy():
    """LiveBuffer._flush (phase live_ingest) must name STREAM_PRIMARY, not DURABLE inline.

    The patient 10s/2 retry lives on the deferred repair/backfill path; putting
    it inline would stall the serial live writer behind one slow provider response.
    """
    src = (PKG / "ingest.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="ingest.py")
    live_policies = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name != "embed_for_write":
            continue
        if _const_of(node, "phase") != "live_ingest":
            continue
        live_policies.append((_policy_name_of(node), node.lineno))
    assert live_policies, "no phase='live_ingest' embed_for_write call found in ingest.py"
    for policy, lineno in live_policies:
        assert policy == "STREAM_PRIMARY_EMBED_POLICY", (
            f"ingest.py:{lineno} phase='live_ingest' names policy={policy!r}; "
            "the live writer primary must be STREAM_PRIMARY_EMBED_POLICY (5s/0), "
            "with DURABLE_WRITE_EMBED_POLICY (10s/2) reserved for the deferred path"
        )


def test_realtime_paths_do_not_use_stream_primary():
    """The 5s/0 primary is a writer policy — realtime recall stays 3s/0.

    Neither direction of the split may leak: durable sites must not take the
    realtime budget (covered above), and realtime query sites must not take the
    writer's 5s budget either.
    """
    from v3core.embedding import REALTIME_EMBED_POLICY

    assert REALTIME_EMBED_POLICY.name == "realtime"
    assert REALTIME_EMBED_POLICY.timeout == 3.0
    assert REALTIME_EMBED_POLICY.retries == 0

    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name not in REALTIME_BY_DESIGN:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name not in ("call_embedding", "embed_for_write"):
                continue
            if _policy_name_of(node) == "STREAM_PRIMARY_EMBED_POLICY":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "realtime recall call sites selecting the writer primary policy:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
