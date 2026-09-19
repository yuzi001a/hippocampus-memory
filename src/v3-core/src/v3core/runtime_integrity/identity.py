"""Content identity for the runtime-integrity layer.

Primary comparator: a canonical content fingerprint over package code files.
Two scopes:

  - ``critical``: package ``__init__`` + capability-marker files (fast path).
  - ``full``: every package file except ``__pycache__``/``*.pyc``/dist-info.

The same fingerprints are computed from a directory (installed copy) and
from a wheel zip (approved artifact), so equality means byte-identity of
code content — versions are irrelevant to the comparison.

Hash method for every file and for the aggregate:

    sha256 over raw bytes; aggregate =
    sha256( "\\n".join(sorted("name:sha256hex")) ) with names
    normalized to ``<package>/<relpath>`` with forward slashes.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

SCOPE_CRITICAL = "critical"
SCOPE_FULL = "full"

_EXCLUDED_DIR_PARTS = {"__pycache__"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

# Critical files, relative to the package root. Missing files are skipped
# (their absence is itself a capability signal, reported separately).
_CRITICAL_RELPATHS = (
    "__init__.py",
    "embed_chunks.py",
    "embedding.py",
    "tokenizer.py",
    "schema/qa_embedding_chunks.sql",
    "reliability/__init__.py",
)


def sha256_file(path: Path, blocksize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(blocksize), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _agg(pairs: list[tuple[str, str]]) -> str:
    body = "\n".join(f"{name}:{digest}" for name, digest in sorted(pairs))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _is_code_file(relparts: tuple[str, ...]) -> bool:
    if any(p in _EXCLUDED_DIR_PARTS for p in relparts):
        return False
    if relparts and Path(relparts[-1]).suffix in _EXCLUDED_SUFFIXES:
        return False
    return True


def fingerprint_directory(package_root: Path, package_name: str, scope: str = SCOPE_FULL):
    """Return ``(fingerprint, file_count)`` for an installed package root.

    ``package_name`` is prepended to every relative path so the name space
    matches wheel-zip entries (``v3core/__init__.py``).
    """
    pairs: list[tuple[str, str]] = []
    if scope == SCOPE_CRITICAL:
        for rel in _CRITICAL_RELPATHS:
            f = package_root / rel
            if f.is_file():
                pairs.append((f"{package_name}/{rel}", sha256_file(f)))
    else:
        for f in sorted(package_root.rglob("*")):
            if not f.is_file():
                continue
            relparts = f.relative_to(package_root).parts
            if not _is_code_file(relparts):
                continue
            rel = "/".join(relparts)
            pairs.append((f"{package_name}/{rel}", sha256_file(f)))
    if not pairs:
        return None, 0
    return _agg(pairs), len(pairs)


def fingerprint_wheel(wheel_path: Path, package_name: str, scope: str = SCOPE_FULL):
    """Return ``(fingerprint, file_count)`` for the ``package_name`` tree
    inside a wheel zip. Comparison space matches ``fingerprint_directory``.
    """
    pairs: list[tuple[str, str]] = []
    with zipfile.ZipFile(wheel_path) as z:
        names = [n for n in z.namelist() if n.startswith(package_name + "/") and not n.endswith("/")]
        for name in sorted(names):
            relparts = tuple(name.split("/")[1:])  # drop package prefix, keep inner parts
            if not _is_code_file(relparts):
                continue
            if scope == SCOPE_CRITICAL and "/".join(relparts) not in _CRITICAL_RELPATHS:
                continue
            digest = sha256_bytes(z.read(name))
            pairs.append((name, digest))
    if not pairs:
        return None, 0
    return _agg(pairs), len(pairs)


def capabilities_from_source(init_text: str, package_root: Path | None = None) -> dict:
    """Capability markers used for cheap human-readable identity checks."""
    caps: dict = {
        "marker_writer_generation": "unknown",
        "embed_chunks_capability": "embed_chunks.py" in init_text or False,
        "reliability_present": False,
    }
    if "in_flight" in init_text:
        caps["marker_writer_generation"] = "new"
    elif "embedding_error_fingerprint" in init_text:
        caps["marker_writer_generation"] = "legacy"
    # The import statement for embed_chunks appears in the new generation.
    if "embed_chunks" in init_text:
        caps["embed_chunks_capability"] = True
    if package_root is not None:
        caps["reliability_present"] = (package_root / "reliability").is_dir()
        caps["embed_chunks_module"] = (package_root / "embed_chunks.py").is_file()
    return caps


def capabilities_from_dir(package_root: Path) -> dict:
    init = package_root / "__init__.py"
    text = ""
    if init.is_file():
        try:
            text = init.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    return capabilities_from_source(text, package_root)


def capabilities_from_wheel(wheel_path: Path, package_name: str) -> dict:
    init_name = f"{package_name}/__init__.py"
    text = ""
    has_embed_chunks = False
    has_reliability = False
    with zipfile.ZipFile(wheel_path) as z:
        for n in z.namelist():
            if n == init_name:
                text = z.read(n).decode("utf-8", errors="replace")
            elif n == f"{package_name}/embed_chunks.py":
                has_embed_chunks = True
            elif n.startswith(f"{package_name}/reliability/"):
                has_reliability = True
    caps = capabilities_from_source(text, None)
    caps["embed_chunks_module"] = has_embed_chunks
    caps["reliability_present"] = has_reliability
    return caps


def wheel_file_sha256(wheel_path: Path) -> str:
    return sha256_file(wheel_path)
