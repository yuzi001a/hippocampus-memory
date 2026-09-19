"""Same-environment import resolution.

Runs a stdlib-only probe under an EXACT environment (never sanitized) and
reports which files ``v3core`` / ``v3hermes`` would resolve to.

The probe deliberately avoids importing the target packages (no execution
of package code, no side effects in production processes' environments):
it uses ``importlib.util.find_spec`` + ``importlib.metadata`` + source-file
reads only. The parsed probe payload is prefixed ``__PROBE_JSON__`` so the
JSON can be extracted even if unrelated output appears on stdout.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .models import ResolutionResult


_PROBE_CODE = r'''
import hashlib, json, sys, importlib.util

def _sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()

_CRIT = ("__init__.py", "embed_chunks.py", "embedding.py", "tokenizer.py",
         "schema/qa_embedding_chunks.sql", "reliability/__init__.py")

out = {"python_executable": sys.executable, "sys_path_head": sys.path[:8]}

def _resolve(pkg):
    try:
        spec = importlib.util.find_spec(pkg)
    except Exception as e:
        return {"error": repr(e)}
    if spec is None:
        return {"error": "not found"}
    origin = spec.origin
    if origin is None:
        # namespace package
        return {"origin": None, "locations": list(spec.submodule_search_locations or [])}
    return {"origin": origin}

def _fingerprint(root):
    pairs = []
    for rel in _CRIT:
        f = root / rel
        if f.is_file():
            pairs.append("v3core/" + rel + ":" + _sha256(str(f)))
    if not pairs:
        return None, 0
    import hashlib as _h
    agg = _h.sha256("\n".join(sorted(pairs)).encode("utf-8")).hexdigest()
    return agg, len(pairs)

for pkgname in ("v3core", "v3hermes"):
    info = _resolve(pkgname)
    out[pkgname + "_file"] = info.get("origin")
    out[pkgname + "_error"] = info.get("error")
    version = None
    try:
        import importlib.metadata as md
        dist = {"v3core": "v3-core", "v3hermes": "v3-hermes-plugin"}[pkgname]
        version = md.version(dist)
    except Exception:
        version = None
    out[pkgname + "_version"] = version

# fingerprint + capabilities for v3core only (content identity carrier)
core_file = out.get("v3core_file")
out["fingerprint"] = None
out["fingerprint_scope"] = None
out["capabilities"] = {}
if core_file:
    import pathlib
    root = pathlib.Path(core_file).parent
    fp, n = _fingerprint(root)
    out["fingerprint"] = fp
    out["fingerprint_scope"] = "critical" if fp else None
    text = ""
    init = root / "__init__.py"
    if init.is_file():
        try:
            text = init.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
    gen = "unknown"
    if "in_flight" in text:
        gen = "new"
    elif "embedding_error_fingerprint" in text:
        gen = "legacy"
    out["capabilities"] = {
        "marker_writer_generation": gen,
        "embed_chunks_module": (root / "embed_chunks.py").is_file(),
        "reliability_present": (root / "reliability").is_dir(),
    }
    out["v3core_package_root"] = str(root)

print("__PROBE_JSON__" + json.dumps(out))
'''


def probe_environment(
    python_executable: str,
    *,
    pythonpath: str,
    cwd: str | None = None,
    timeout: float = 120.0,
) -> ResolutionResult:
    """Resolve v3core/v3hermes under ``python_executable`` with the EXACT
    ``pythonpath`` supplied (empty string means "no PYTHONPATH", faithfully).

    The rest of the environment is inherited; only PYTHONPATH is set to the
    supplied value — matching the live process being reproduced.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [python_executable, "-c", _PROBE_CODE],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as exc:
        return ResolutionResult(
            python_executable=python_executable,
            pythonpath=pythonpath,
            cwd=cwd,
            error=f"probe launch failed: {exc!r}",
        )
    payload = None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("__PROBE_JSON__"):
            try:
                payload = json.loads(line[len("__PROBE_JSON__"):])
            except Exception:
                payload = None
    if payload is None:
        return ResolutionResult(
            python_executable=python_executable,
            pythonpath=pythonpath,
            cwd=cwd,
            error=f"probe returned no JSON (rc={r.returncode}): {(r.stderr or '')[-300:]}",
        )
    return ResolutionResult(
        python_executable=str(payload.get("python_executable") or python_executable),
        pythonpath=pythonpath,
        cwd=cwd,
        v3core_file=payload.get("v3core_file"),
        v3hermes_file=payload.get("v3hermes_file"),
        v3core_version=payload.get("v3core_version"),
        v3hermes_version=payload.get("v3hermes_version"),
        v3core_package_root=payload.get("v3core_package_root"),
        fingerprint=payload.get("fingerprint"),
        fingerprint_scope=payload.get("fingerprint_scope"),
        capabilities=payload.get("capabilities") or {},
        sys_path_head=list(payload.get("sys_path_head") or []),
        error=payload.get("v3core_error"),
    )


def probe_import_source_static(python_executable: str, pythonpath: str) -> dict:
    """Lightweight variant used by tests: run the same probe and return the
    raw payload dict (or {\"error\": ...})."""
    res = probe_environment(python_executable, pythonpath=pythonpath)
    if res.error and res.v3core_file is None:
        return {"error": res.error}
    return {
        "v3core_file": res.v3core_file,
        "v3hermes_file": res.v3hermes_file,
        "fingerprint": res.fingerprint,
        "capabilities": res.capabilities,
    }
