"""LoCoMo Recall v2 dataset/manifest/lab-tooling slice (G6C-A).

Development-only evaluator infrastructure. This package hosts the
external-source-only LoCoMo loader, the deterministic manifest
builder, the disposable-lab safety guard, and the offline JSONL
result diff.

This module is intentionally small and explicit:

  * It does NOT import the private LoCoMo build script at runtime.
    The dataset loader re-implements the tested ``eval_v2`` rules
    directly so the public worktree stays portable.
  * It does NOT touch production src/v3core, G5B files, docs,
    configs, reports, or raw benchmark data.
  * It does NOT commit, push, or open a real PG/provider
    connection. Lab DSNs are guarded by :mod:`lab` and refused
    unless they look disposable + loopback-only.

See ``README``/``AGENTS`` notes for the public contract. The
implementation here is a development infrastructure slice that
the parent adapter imports; this slice owns only dataset shape,
manifest format, lab safety, and result diffing.
"""
from __future__ import annotations

__version__ = "0.1.0-dev"

__all__ = [
    "dataset",
    "manifest",
    "lab",
    "compare",
]