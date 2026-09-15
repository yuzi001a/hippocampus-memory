# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 CLI — module entry point.

This is the durable, single documented entry point:

    python -m eval.locomo_recall_v2 run --help

The handler delegates to :func:`eval.locomo_recall_v2.cli.main`
so the package can be executed as ``python -m
eval.locomo_recall_v2``.

Both ``python -m eval.locomo_recall_v2`` and
``python -m eval.locomo_recall_v2 run`` are accepted; the
default subcommand is ``run`` so callers can omit it.
"""
from __future__ import annotations

import sys

from .cli import main


def _module_main() -> int:
    argv = sys.argv[1:]
    # ``python -m eval.locomo_recall_v2`` (no subcommand) is
    # treated as ``python -m eval.locomo_recall_v2 run`` so the
    # documented command is the default.  A plain ``--help`` /
    # ``-h`` also forwards to argparse so the help text is
    # printed by argparse (which exits with code 0).
    if argv and argv[0] in {"run", "-h", "--help"}:
        return main(argv)
    return main(["run", *argv])


if __name__ == "__main__":
    raise SystemExit(_module_main())