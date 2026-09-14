"""G5b REAL MEMORY EVALUATOR v1 (development infrastructure, not production).

This package is a development-only evaluator for the
``v3core.active_memory_store`` clean-boundary contract. It does
NOT modify the canonical writer/reader; it does NOT write production
config/PG; it does NOT start or stop services.

Two explicit lanes:

* **Lane A (deterministic in-memory):** a small SQLite-backed fake
  implementing the ``pool.lease(timeout=...)`` contract and the
  ``explicit_memories`` SQL surface. Always available. Repeatable.
  Embeddings come from a deterministic hash-based stub unless the
  caller passes an injected embedder.

* **Lane B (live provider-backed PG):** plumbing is disabled by
  default and **fails closed** unless ``--live-pg`` is supplied AND
  ``G5B_EVAL_LAB_DSN`` (or ``--dsn``) points at a non-production
  disposable DSN. The lab never inherits production credentials,
  the production profile, HOME, a reserved production port, or
  reserved production hostnames.

Honest baseline: Lane A is a **deterministic lab measurement**, not a
production-runtime measurement. Lane B (only when explicitly enabled)
is the closest thing to a real-runtime measurement but still bounded
to a disposable lab database. Any claim of "provider-backed quality"
from Lane A is explicitly false.

See ``README.md`` in this package for the run instructions, scenario
format, and the failure-taxonomy contract.
"""
from __future__ import annotations

__version__ = "1.0.0-dev"