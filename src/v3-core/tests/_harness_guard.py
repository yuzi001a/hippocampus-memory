# -*- coding: utf-8 -*-
"""Fail-closed filesystem-isolation guard for v3-core tests / probes / eval harnesses.

WHY THIS MODULE EXISTS
======================
A harness built ``LiveBuffer(pg=FakePG(), config=<plain dict>)``. The fake pg isolated
the DATABASE but not the FILESYSTEM: ``LiveBuffer._persist_live_item`` writes its durable
outbox under ``_resolve_data_dir(self._pg_config)``, and with a plain dict that falls
through to the REAL production profile root
``C:\\Users\\<user>\\.v3-core\\profiles\\default``. The harness wrote 9 marker files into
the live production outbox; the next gateway restart would have recovered them via
``_recover_live_pending()`` and inserted them as bogus production rows.

    *** A fake DB does NOT imply a fake filesystem. ***

TWO KINDS OF IMPORT SITE (the reason a naive patch is not enough)
===============================================================
``_resolve_data_dir`` is reached through two different bindings:

  (a) MODULE-LEVEL alias, bound ONCE at import time::

          # v3core/ingest.py:32
          from .config import resolve_config, _resolve_data_dir

      ``v3core.ingest._resolve_data_dir`` is a *separate binding* from
      ``v3core.config._resolve_data_dir``. Patching only ``v3core.config`` leaves the
      ingest binding pointing at the original function → the marker still reaches
      production. This is the exact hole that was exploited.

  (b) FUNCTION-LOCAL import, bound AT CALL TIME::

          # v3core/ingest.py:183, v3core/observer.py:4683, v3core/prefetch.py:41, ...
          def enqueue(...):
              from .config import _resolve_data_dir

      Patching ``v3core.config._resolve_data_dir`` DOES cover these.

A guard that patches only one kind is INCOMPLETE. :func:`isolate` therefore sweeps
``sys.modules`` for every ``v3core*`` module object carrying an attribute literally named
``_resolve_data_dir`` and patches each one (plus ``v3core.config`` itself). No module list
is hardcoded — a new module with a module-level alias is covered automatically.

Importable from BOTH the pytest domain (``tests/conftest.py``) and standalone scripts
(``eval/*.py``): nothing here imports pytest, and nothing here touches ``src/v3core/**``.

DANGEROUS ESCAPE HATCH
======================
``V3_HARNESS_ALLOW_REAL_DATA_DIR=1`` makes :func:`isolate` yield the REAL production path
instead of a sandbox. It exists only so an explicitly-authorized maintenance script can
opt in. It is OFF by default and must never be set in CI or in a test run: with it set,
every write the guarded code performs lands in the live production profile.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

__all__ = [
    "HarnessIsolationError",
    "PROD_ROOTS",
    "REPRESENTATIVE_CONFIG",
    "real_home",
    "production_roots",
    "production_profile_dir",
    "production_outbox_dir",
    "guarded_outbox_paths",
    "is_inside_production",
    "assert_isolated",
    "isolate",
    "tree_fingerprint",
    "tree_names",
    "tree_recent",
    "ALLOW_REAL_ENV_VAR",
]

logger = logging.getLogger(__name__)

#: Set to "1" to let :func:`isolate` hand out the REAL production data dir. DANGEROUS.
ALLOW_REAL_ENV_VAR = "V3_HARNESS_ALLOW_REAL_DATA_DIR"

#: Set to "1" to keep the sandbox directory after :func:`isolate` exits (debugging only).
KEEP_SANDBOX_ENV_VAR = "V3_HARNESS_KEEP_SANDBOX"

#: Set to "1" to let the conftest outbox-tree guard DOWNGRADE a detected change into a
#: warning instead of a failure. Needed only on a host where the REAL production gateway
#: is running: it legitimately writes `j/journal_<date>/*.md` (and live-buffer markers)
#: while the suite runs, and a before/after tree comparison cannot tell that apart from a
#: test-caused write. OFF by default — with it unset the guard fails closed.
ALLOW_LIVE_OUTBOX_ENV_VAR = "V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX"

#: Env vars that can point the process at a real production location.
#: ``V3CORE_HOME`` is read by ``v3core.config._find_env``; the other two are the
#: conventional data-root overrides. Any of them set to a live path is treated as
#: production so a harness cannot smuggle production back in through the environment.
_ENV_ROOT_VARS = ("V3CORE_HOME", "V3_DATA_DIR", "V3CORE_DATA_DIR")

#: The config shape that caused the escape: a plain dict (not ``V3Config``) with no
#: ``basePath``. Used as the representative config when verifying that a patch took.
REPRESENTATIVE_CONFIG: dict = {
    "storage": {"embed": {"endpoint": "http://embed.test/v1", "model": "BAAI/bge-m3"}}
}

_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class HarnessIsolationError(RuntimeError):
    """Raised when a harness/test path resolves inside the REAL production profile.

    Fail closed: the caller must abort rather than continue with production as its
    data root.
    """


# ─────────────────────────────────────────────────────────────────────────────
# real home (captured ONCE, at import time, before any monkeypatching)
# ─────────────────────────────────────────────────────────────────────────────
def _looks_like_pytest_tmp(path: Path) -> bool:
    """True when ``path`` looks like a pytest tmp_path root rather than a real home.

    Defensive only: the pytest-domain ``conftest`` imports this module at collection
    time (before its fake-HOME session fixture runs), so ``Path.home()`` is normally
    already real here. This check protects the lazy-import case.
    """
    try:
        s = str(path).lower()
    except Exception:
        return False
    return "v3-test-sandbox" in s or "pytest-of-" in s or "pytest-" in s


def _capture_real_home() -> Path:
    """Capture the real user home directory at import time."""
    candidate: Path | None = None
    try:
        candidate = Path.home()
    except Exception:
        candidate = None
    if candidate is not None and not _looks_like_pytest_tmp(candidate):
        return candidate
    # Path.home() was already redirected (lazy import inside a patched test domain):
    # fall back to the process environment, which the fake-HOME fixture does not touch.
    for var in ("USERPROFILE", "HOME"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    if candidate is not None:
        return candidate
    return Path(os.path.expanduser("~"))


_REAL_HOME: Path = _capture_real_home()


def real_home() -> Path:
    """The real user home, captured at import time (never re-read)."""
    return _REAL_HOME


# ─────────────────────────────────────────────────────────────────────────────
# production roots
# ─────────────────────────────────────────────────────────────────────────────
def _env_derived_roots() -> list[Path]:
    """Roots implied by env vars that point at a real production location."""
    roots: list[Path] = []
    for var in _ENV_ROOT_VARS:
        value = os.environ.get(var)
        if not value:
            continue
        try:
            base = Path(value)
        except Exception:
            continue
        for r in (base, base / ".v3-core", base / ".v3-core" / "profiles" / "default"):
            if r not in roots:
                roots.append(r)
    return roots


def production_roots() -> list[Path]:
    """All roots that count as production: real home + any env-derived root.

    Computed fresh on each call so an env var set later in the process is still caught.
    """
    base = _REAL_HOME / ".v3-core"
    roots: list[Path] = [base, base / "profiles" / "default"]
    for r in _env_derived_roots():
        if r not in roots:
            roots.append(r)
    return roots


#: Real production roots, computed ONCE at import time from the REAL home.
PROD_ROOTS: list[Path] = production_roots()


def production_profile_dir() -> Path:
    """The real production profile root (``~/.v3-core/profiles/default``)."""
    return _REAL_HOME / ".v3-core" / "profiles" / "default"


def production_outbox_dir() -> Path:
    """The real production durable outbox root (``<prod profile>/j``)."""
    return production_profile_dir() / "j"


def guarded_outbox_paths() -> list[Path]:
    """The production paths whose tree fingerprint must never change during a test.

    ``<prod>/j`` itself is included so that a brand-new journal/live-buffer directory
    shows up as a change even when the three known subdirs are untouched.
    """
    j = production_outbox_dir()
    return [
        j / "pending_live_buffer",
        j / "accepted_live_buffer",
        j / "_lost",
        j,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# path predicates
# ─────────────────────────────────────────────────────────────────────────────
def _safe_resolve(path) -> Path:
    """``Path.resolve()`` that never raises (non-existent paths included)."""
    try:
        return Path(path).resolve()
    except Exception:
        return Path(os.path.abspath(str(path)))


def _same_or_under(child: Path, root: Path) -> bool:
    """Case-correct (Windows) equality-or-descendant test on already-resolved paths."""
    c = os.path.normcase(str(child))
    r = os.path.normcase(str(root)).rstrip("\\/")
    if not r:
        return False
    return c == r or c.startswith(r + os.sep)


def is_inside_production(path) -> bool:
    """True if ``path`` is, or is a descendant of, any production root.

    Uses ``Path.resolve()`` and tolerates non-existent paths (returns a bool, never
    raises for a missing file).
    """
    resolved = _safe_resolve(path)
    for root in production_roots():
        if _same_or_under(resolved, _safe_resolve(root)):
            return True
    return False


def assert_isolated(resolved, *, what: str = "data dir") -> Path:
    """Fail closed unless ``resolved`` is OUTSIDE every production root.

    Returns the resolved path when it is safe; raises :class:`HarnessIsolationError`
    naming the offending path, ``what`` and the remedy otherwise. Any internal error
    while deciding raises as well — this function never silently returns.
    """
    try:
        resolved_path = _safe_resolve(resolved)
    except Exception as exc:  # pragma: no cover - defensive
        raise HarnessIsolationError(
            f"harness isolation: cannot resolve {resolved!r} for {what} ({exc!r}) — "
            f"failing closed"
        ) from exc
    try:
        inside = is_inside_production(resolved_path)
    except Exception as exc:  # pragma: no cover - defensive
        raise HarnessIsolationError(
            f"harness isolation: cannot decide whether {resolved_path} for {what} is "
            f"inside production ({exc!r}) — failing closed"
        ) from exc
    if inside:
        raise HarnessIsolationError(
            f"harness isolation breach: {what} resolved to {resolved_path}, which is "
            f"inside the REAL production profile tree "
            f"{[str(r) for r in production_roots()]}. "
            f"Remedy: wrap the code under test in "
            f"`with isolate():` from tests/_harness_guard.py — a fake DB does NOT imply "
            f"a fake filesystem. Setting {ALLOW_REAL_ENV_VAR}=1 is DANGEROUS and only "
            f"for an explicitly-authorized maintenance script."
        )
    return resolved_path


# ─────────────────────────────────────────────────────────────────────────────
# tree fingerprint
#
# WHY THIS WALK IS WRITTEN THIS WAY (measured, not guessed)
# =========================================================
# ``tests/conftest.py`` fingerprints the four guarded production paths (and calls
# ``tree_names`` on each of them) BEFORE and AFTER every single test — 8
# fingerprints + 4 name listings, i.e. ~12 full walks per test. The live outbox
# holds ~10k files. Measured cost of the previous implementation on this host:
# 737-1079 ms per ``tree_fingerprint`` on ``accepted_live_buffer`` (9156-9223
# files), 853-1528 ms on a 10k-file tmp tree, 1140-1197 ms per ``tree_names`` on
# the whole ``j`` tree — about 4.9 s of pure guard overhead per test, which is
# what made the suite unusable. It was expensive because it built a ``Path`` and
# ran ``relative_to().as_posix()`` for EVERY entry and then materialised one
# giant ``"\n".join(names)`` string to sha256 in one shot.
#
# The walk below keeps the exact same semantics but:
#   * reads each directory ONCE via ``os.scandir()`` and takes mtime/kind from the
#     ``DirEntry``'s cached stat — each entry is stat'ed at most once;
#   * builds the relative name by concatenating the walk prefix (no Path churn);
#   * folds the SORTED entries into a rolling ``hashlib.sha256`` incrementally
#     (a small update per entry) instead of joining 10k names into one string.
#
# Detection power is unchanged. The digest covers each entry's NAME and KIND
# (file/dir) but deliberately NOT its mtime_ns: on Windows a directory's mtime is
# flushed LAZILY (only when the last handle to it closes), so a freshly written
# file can bump its parent directory's mtime several calls later with no
# user-visible event. Folding mtime into the digest made two back-to-back calls
# over an unchanged tree disagree — caught by the pre-existing
# ``test_tree_fingerprint_detects_add_and_remove``, which asserts a stable
# fingerprint when nothing happens. mtime stays in ``max_mtime_ns`` (element [2]
# of the tuple), exactly as before, which is where the touch detection lives.
# ─────────────────────────────────────────────────────────────────────────────
_KIND_FILE = b"f"
_KIND_DIR = b"d"


def _walk_tree(root: Path) -> tuple:
    """One pass over ``root`` → ``(entries, max_mtime_ns)``.

    ``entries`` is a list of ``(relative_posix_name, mtime_ns, is_dir)`` covering
    every descendant — files AND directories — sorted by name. ``root`` itself is
    NOT an entry (``count`` therefore means "descendants", as before). Never
    raises: an unreadable directory is skipped and an entry whose stat fails
    contributes ``mtime_ns == 0``.
    """
    entries: list[tuple[str, int, bool]] = []
    append = entries.append
    max_mtime_ns = 0
    # (directory to read, posix prefix of everything inside it)
    stack: list[tuple[str, str]] = [(os.fspath(root), "")]
    while stack:
        dir_path, prefix = stack.pop()
        try:
            scanner = os.scandir(dir_path)
        except OSError:
            continue
        try:
            for entry in scanner:
                # DirEntry.stat()/is_dir() reuse the cached info from the
                # directory listing (no second syscall per entry on Windows).
                try:
                    st = entry.stat(follow_symlinks=False)
                    mtime_ns = st.st_mtime_ns
                    is_dir = stat.S_ISDIR(st.st_mode)
                except OSError:
                    mtime_ns = 0
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                rel = prefix + entry.name
                append((rel, mtime_ns, is_dir))
                if mtime_ns > max_mtime_ns:
                    max_mtime_ns = mtime_ns
                if is_dir:
                    stack.append((entry.path, rel + "/"))
        finally:
            scanner.close()
    entries.sort()
    return entries, max_mtime_ns


def tree_names(root) -> list[str]:
    """Sorted relative names of every descendant — ``[]`` for a missing dir, never raises.

    Turns a fingerprint mismatch into an actionable diff (which paths appeared or
    disappeared).
    """
    try:
        root_path = Path(root)
        if not root_path.is_dir():
            return []
        entries, _ = _walk_tree(root_path)
        return [rel for rel, _mtime_ns, _is_dir in entries]
    except Exception:  # pragma: no cover - defensive
        return []


def tree_recent(root, since_mtime_ns: int, limit: int = 8) -> list[str]:
    """Relative names whose mtime is NEWER than ``since_mtime_ns`` (cap ``limit``).

    Diagnostic only: identifies which descendants were written during the window
    between a ``before`` fingerprint's ``max_mtime_ns`` and now. Never raises.
    """
    try:
        root_path = Path(root)
        if not root_path.is_dir():
            return []
        entries, _ = _walk_tree(root_path)  # already sorted by name
        hits = [rel for rel, mtime_ns, _is_dir in entries if mtime_ns > since_mtime_ns]
        return hits[:limit]
    except Exception:  # pragma: no cover - defensive
        return []


def tree_fingerprint(root) -> tuple:
    """Cheap fingerprint of a directory tree: ``(count, digest, max_mtime_ns)``.

    ``count``/``max_mtime_ns`` cover all descendants (files AND directories), so both a
    new file and a touched directory change the value. A missing directory returns a
    stable empty fingerprint ``(0, sha256(b""), 0)`` and this function never raises:
    an internal error yields ``count == -1``, which can never equal a healthy
    fingerprint, so a before/after comparison fails loudly instead of silently.

    The digest folds the SORTED entries incrementally — each descendant's name and
    kind — so it does not depend on the order the filesystem returns entries in,
    and it changes on an addition and a removal alike. A touch is caught by
    ``max_mtime_ns`` (element ``[2]``), as before.
    """
    try:
        root_path = Path(root)
        if not root_path.is_dir():
            return (0, _EMPTY_DIGEST, 0)
        entries, max_mtime_ns = _walk_tree(root_path)
        if not entries:
            return (0, _EMPTY_DIGEST, max_mtime_ns)
        digest = hashlib.sha256()
        update = digest.update
        for rel, _mtime_ns, is_dir in entries:
            update(rel.encode("utf-8", "surrogatepass"))
            update(_KIND_DIR if is_dir else _KIND_FILE)
        return (len(entries), digest.hexdigest(), max_mtime_ns)
    except Exception as exc:  # pragma: no cover - defensive
        return (-1, f"error:{type(exc).__name__}", 0)


# ─────────────────────────────────────────────────────────────────────────────
# isolate()
# ─────────────────────────────────────────────────────────────────────────────
def _resolver_targets() -> dict:
    """Every ``v3core*`` module object carrying an attribute named ``_resolve_data_dir``.

    Enumerated from ``sys.modules`` — deliberately NOT a hardcoded list, so a new module
    with a module-level ``from .config import _resolve_data_dir`` alias is covered the
    day it is written.
    """
    targets: dict = {}
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if not (name == "v3core" or name.startswith("v3core")):
            continue
        try:
            if hasattr(module, "_resolve_data_dir"):
                targets[name] = module
        except Exception:
            continue
    return targets


def _safe_slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(text))
    return (out or "harness")[:40]


def _verify_isolation(targets: dict, sandbox: Path) -> None:
    """Fail closed unless EVERY patched resolver returns a path inside ``sandbox``."""
    problems: list[str] = []
    if not targets:
        problems.append(
            "no module exposing _resolve_data_dir was found in sys.modules — "
            "the guard cannot prove isolation"
        )
    for name, module in targets.items():
        for label, call in (
            ("representative config", lambda m=module: m._resolve_data_dir(REPRESENTATIVE_CONFIG)),
            ("no-arg production default", lambda m=module: m._resolve_data_dir()),
            ("None config", lambda m=module: m._resolve_data_dir(None)),
        ):
            try:
                got = call()
            except Exception as exc:
                problems.append(f"{name}._resolve_data_dir({label}) raised {exc!r}")
                continue
            resolved = _safe_resolve(got)
            if not _same_or_under(resolved, sandbox):
                problems.append(
                    f"{name}._resolve_data_dir({label}) -> {resolved} is OUTSIDE the "
                    f"sandbox {sandbox}"
                )
    if problems:
        raise HarnessIsolationError(
            "harness isolation FAILED to take effect (fail closed): "
            + "; ".join(problems)
            + f". Sandbox={sandbox}; targets={sorted(targets)}"
        )


@contextlib.contextmanager
def isolate(what: str = "harness"):
    """Redirect every ``_resolve_data_dir`` to a fresh temp sandbox for the block.

    On enter:
      1. ``tempfile.mkdtemp(prefix="v3-harness-<what>-")`` becomes the sandbox root;
      2. EVERY module attribute named ``_resolve_data_dir`` in the ``v3core*`` namespace
         (found by scanning ``sys.modules``, plus ``v3core.config`` itself) is patched to
         return a path under the sandbox — covering BOTH the module-level-alias kind and
         the function-local-import kind;
      3. the patch is VERIFIED by calling each patched resolver (representative config,
         ``None`` and no-arg) and asserting the result is inside the sandbox; anything
         else raises :class:`HarnessIsolationError` immediately, before the body runs.

    Yields the sandbox root ``Path``. On exit every original attribute is restored and
    the sandbox is removed (unless ``V3_HARNESS_KEEP_SANDBOX=1``).

    DANGEROUS: with ``V3_HARNESS_ALLOW_REAL_DATA_DIR=1`` this yields the REAL production
    data dir instead. Never set that in a test run or in CI.

    Import the ``v3core`` modules you intend to exercise *before* entering the block when
    you can: modules imported later still bind the patched ``v3core.config`` attribute,
    but importing first makes the sweep cover their module-level aliases explicitly.
    """
    if os.environ.get(ALLOW_REAL_ENV_VAR) == "1":
        real = _safe_resolve(production_profile_dir())
        logger.warning(
            "[harness-guard] %s=1 — isolate() is yielding the REAL production data dir "
            "%s. This is DANGEROUS and must only be used by an explicitly-authorized "
            "maintenance script.",
            ALLOW_REAL_ENV_VAR, real,
        )
        yield real
        return

    sandbox = Path(tempfile.mkdtemp(prefix=f"v3-harness-{_safe_slug(what)}-"))
    data_root = sandbox / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[object, object]] = []

    def _isolated_resolver(_config=None):
        # Same return TYPE as the real resolver (Path), so callers that do
        # `Path(base) / "j" / ...` or `str(base / ...)` keep working unchanged.
        return data_root

    try:
        targets = _resolver_targets()
        if "v3core.config" not in targets:
            # The canonical module must always be covered, even on a bare interpreter.
            try:
                import v3core.config as _config_mod  # noqa: PLC0415

                targets["v3core.config"] = _config_mod
            except Exception as exc:  # pragma: no cover - defensive
                raise HarnessIsolationError(
                    f"harness isolation: cannot import v3core.config ({exc!r}) — "
                    f"failing closed"
                ) from exc
        for name, module in targets.items():
            try:
                original = getattr(module, "_resolve_data_dir")
            except Exception:
                continue
            saved.append((module, original))
            try:
                setattr(module, "_resolve_data_dir", _isolated_resolver)
            except Exception as exc:
                raise HarnessIsolationError(
                    f"harness isolation: cannot patch {name}._resolve_data_dir "
                    f"({exc!r}) — failing closed"
                ) from exc
        _verify_isolation(targets, sandbox)
        logger.debug(
            "[harness-guard] isolate(%r): sandbox=%s patched=%s",
            what, sandbox, sorted(targets),
        )
        yield sandbox
    finally:
        for module, original in reversed(saved):
            try:
                setattr(module, "_resolve_data_dir", original)
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "[harness-guard] failed to restore _resolve_data_dir on %r",
                    getattr(module, "__name__", module),
                )
        if os.environ.get(KEEP_SANDBOX_ENV_VAR) == "1":
            logger.warning("[harness-guard] keeping sandbox %s (%s=1)",
                           sandbox, KEEP_SANDBOX_ENV_VAR)
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
