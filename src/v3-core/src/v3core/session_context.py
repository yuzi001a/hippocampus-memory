"""轻量的 per-session 临时状态。

本模块故意不依赖 V3Core、Runtime、数据库、线程或任何外部 client。
V3Core 是上下文 registry 的 owner；V3HermesProvider 在没有可用 Core
（例如离线测试/兼容调用）时可以使用同一数据类作为 fallback。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class V3SessionContext:
    """One session's mutable, disposable state only.

    Runtime resources and shared workers must never be added here.  The
    ``summary_lifecycle`` and ``injection_dedupe`` dictionaries contain only
    scalar/list/dict values so the context can be discarded with its session.
    """

    session_id: str
    pending_qa: dict[str, Any] | None = None
    injection_dedupe: dict[str, Any] = field(
        default_factory=lambda: {
            "identity": "",
            "situation": "",
            "first_round_done": False,
            "tail_given": False,
        }
    )
    synced_count: int = 0
    turn: int = 0
    checkpoint_count: int = 0
    msg_counter: int = 0
    summary_lifecycle: dict[str, Any] = field(
        default_factory=lambda: {"state": "active", "pending": 0}
    )
    # Per-session identity set of message keys already seen by sync_turn.
    #
    # Compression-aware source-ingest durability (2026-09-07 fix):
    # the previous positional cursor ``synced_count = len(messages)`` was
    # unsafe because compression shrinks the re-assembled history.  When the
    # provider pushed a compressed history + a new turn, the early-return
    # guard ``len(messages) <= prev_count`` dropped the new turn entirely.
    #
    # ``synced_message_ids`` is the AUTHORITATIVE delta cursor: sync_turn
    # walks the snapshot in order and processes only keys not in the set.
    # ``synced_count`` is retained for backward compatibility and as an
    # observable signal of the most recent snapshot length, but it is no
    # longer used as the delta cursor itself.
    #
    # Invariants enforced via sync_turn:
    #   I1 every eligible turn gets an idempotency key
    #   I2 compression never hides new keys (no early-return on list len)
    #   I3 historical keys are not re-enqueued on no-op replays
    #   I4 unknown keys with stable id are accepted; fuzzy-key fallback is
    #      marked ``reliable=False`` for diagnostics only
    #   I5 only durable target advances the cursor
    #   I6 crash/retry safe — replay of same key is a no-op
    #   I7 the source cursor persists across the Core lifetime: it lives on
    #      this per-session Context, so a same-session ``switch_session``
    #      (reset=True or False) keeps the already-seen keys intact —
    #      ``reset`` only clears injector/identity caches, NEVER the
    #      durable-history cursor.  The cursor is only empty when the
    #      Context itself is brand-new (new session id, fresh Core, or
    #      explicit discard by the caller).
    synced_message_ids: set[str] = field(default_factory=set)


# Short name for callers that do not need the package prefix.
SessionContext = V3SessionContext

__all__ = ["V3SessionContext", "SessionContext"]
