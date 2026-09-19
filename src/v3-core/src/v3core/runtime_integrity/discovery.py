"""Installation discovery for the runtime-integrity layer.

Enumerates every on-disk copy of ``v3core`` / ``v3hermes`` reachable from
KNOWN roots — never a whole-disk walk. Root derivation (task book §4):

  - a supplied Hermes checkout (``hermes-agent``): its ``venv`` and its
    ``.hermes-runtime`` trees
  - ``~/.hermes-runtime``
  - extra roots supplied by the caller (live PYTHONPATH roots, etc.)
  - the running process's own site-packages
  - editable ``.pth`` markers in every candidate site-packages (including
    ``*.pth.disabled*`` variants, which are recorded but flagged inactive)

Each found copy is classified (install_type) and fingerprinted. Directory
names carry no authority — classification is content-first.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .identity import (
    SCOPE_CRITICAL,
    SCOPE_FULL,
    capabilities_from_dir,
    fingerprint_directory,
)
from .models import (
    ApprovedArtifact,
    INSTALL_TYPE_EDITABLE,
    INSTALL_TYPE_LEGACY_INSTALL,
    INSTALL_TYPE_OFFICIAL_RELEASE,
    INSTALL_TYPE_SOURCE_TREE,
    INSTALL_TYPE_UNKNOWN,
    INSTALL_TYPE_WHEEL_UNKNOWN,
    InstallCopy,
)

_PACKAGES = ("v3core", "v3hermes")
_DIST_GLOBS = {
    "v3core": ("v3_core*.dist-info", "v3_core*.egg-info"),
    "v3hermes": ("v3_hermes_plugin*.dist-info", "v3_hermes_plugin*.egg-info"),
}

_EDITABLE_RE = re.compile(r"__editable__[^\n]*v3[^\n]*", re.IGNORECASE)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def _mtime_of_tree(root: Path) -> str | None:
    try:
        return _iso(root.stat().st_mtime)
    except OSError:
        return None


def _version_from_dist_info(site_packages: Path, package: str) -> tuple[str | None, str | None]:
    for glob in _DIST_GLOBS[package]:
        for d in site_packages.glob(glob):
            if not d.is_dir():
                continue
            try:
                name = d.name
                parts = name.split("-")
                ver = parts[1] if len(parts) > 1 else None
                return ver, str(d)
            except Exception:
                return None, str(d)
    return None, None


def _looks_like_source_tree(package_root: Path) -> bool:
    s = str(package_root).replace("\\", "/").lower()
    if "/src/v3-core/src/" in s or "/src/v3-hermes-plugin/src/" in s:
        return True
    # A pyproject.toml two levels up with a src/ layout is the repo shape.
    for parent in list(package_root.parents)[:3]:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return True
    return False


def _editable_targets(site_packages: Path) -> dict[str, str]:
    """Map package -> editable target path declared by any .pth / finder in
    this site-packages (both active and disabled markers are recorded; the
    caller decides activity from resolution, not the marker name)."""
    out: dict[str, str] = {}
    try:
        entries = list(site_packages.glob("*.__editable__*")) + list(site_packages.glob("*__editable__*.pth*"))
        entries += list(site_packages.glob("*editable*v3*"))
        entries += list(site_packages.glob("*editable*.pth*"))
    except OSError:
        return out
    seen: set[str] = set()
    for p in entries:
        sp = str(p)
        if sp in seen or not p.is_file():
            continue
        seen.add(sp)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _EDITABLE_RE.search(text)
        if not m:
            continue
        # finder .py files import a module; pth files may hold a path.
        for pkg in _PACKAGES:
            if pkg in sp.lower() or pkg in text.lower():
                target = None
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith(("import ", "from ", "#")):
                        target = line
                        break
                if target is None:
                    m2 = re.search(r"MAPPING[^\n]*'%s':\s*'([^']+)'" % pkg, text)
                    if m2:
                        target = m2.group(1)
                out.setdefault(pkg, target or sp)
    return out


def candidate_roots(
    *,
    hermes_home: Path | str | None = None,
    checkout: Path | str | None = None,
    extra_roots: list[Path | str] | None = None,
) -> list[tuple[Path, str]]:
    """Return ``(site_packages_or_root, origin_label)`` candidates, deduped.

    Accepts str or Path for every argument (str is coerced) so callers that
    pass raw config values cannot crash discovery.
    """
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    hermes_home = Path(hermes_home) if hermes_home is not None else None
    checkout = Path(checkout) if checkout is not None else None
    extra_roots = [Path(p) for p in (extra_roots or [])]

    def add(p: Path | None, origin: str) -> None:
        if p is None:
            return
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        key = str(rp).lower()
        if key in seen:
            return
        seen.add(key)
        out.append((p, origin))

    checkouts: list[Path] = []
    if checkout is not None:
        checkouts.append(checkout)
    if hermes_home is not None:
        cand = hermes_home / "hermes-agent"
        if cand.is_dir():
            checkouts.append(cand)

    for co in checkouts:
        add(co / "venv" / "Lib" / "site-packages", "venv_site_packages")
        rt_base = co / ".hermes-runtime" / "python"
        if rt_base.is_dir():
            try:
                for py_dir in sorted(rt_base.glob("cpython-*")):
                    sp = py_dir / "Lib" / "site-packages"
                    if sp.is_dir():
                        add(sp, "hermes_runtime_site_packages")
            except OSError:
                pass
        # repo source tree shape (editable-source case)
        for pkg in _PACKAGES:
            src_candidates = {
                "v3core": co / "src" / "v3-core" / "src" / "v3core",
                "v3hermes": co / "src" / "v3-hermes-plugin" / "src" / "v3hermes",
            }
            s = src_candidates[pkg]
            if s.is_dir():
                add(s, "source_tree")

    home_rt = Path.home() / ".hermes-runtime" / "python"
    if home_rt.is_dir():
        try:
            for py_dir in sorted(home_rt.glob("cpython-*")):
                sp = py_dir / "Lib" / "site-packages"
                if sp.is_dir():
                    add(sp, "hermes_runtime_site_packages")
        except OSError:
            pass

    for extra in extra_roots or []:
        try:
            e = Path(extra)
        except TypeError:
            continue
        if e.is_dir():
            add(e, "extra_root")

    return out


def discover_copies(
    *,
    hermes_home: Path | None = None,
    checkout: Path | None = None,
    extra_roots: list[Path] | None = None,
    approved: ApprovedArtifact | None = None,
    scope: str = SCOPE_CRITICAL,
) -> list[InstallCopy]:
    """Find and classify every reachable copy. Deterministic order."""
    copies: list[InstallCopy] = []
    seen_pkg_roots: set[str] = set()
    hermes_home = Path(hermes_home) if hermes_home is not None else None
    checkout = Path(checkout) if checkout is not None else None
    extra_roots = [Path(p) for p in (extra_roots or [])]
    roots = candidate_roots(hermes_home=hermes_home, checkout=checkout, extra_roots=extra_roots)
    for root, origin in roots:
        for pkg in _PACKAGES:
            pkg_root = root / pkg
            if not pkg_root.is_dir():
                continue
            try:
                dedup_key = str(pkg_root.resolve()).lower()
            except OSError:
                dedup_key = str(pkg_root).lower()
            if dedup_key in seen_pkg_roots:
                continue
            seen_pkg_roots.add(dedup_key)
            ver, dist_path = _version_from_dist_info(root, pkg)
            editable = _editable_targets(root)
            caps = capabilities_from_dir(pkg_root)
            fp, count = fingerprint_directory(pkg_root, pkg, scope=scope)

            if pkg in editable and _looks_like_source_tree(pkg_root):
                itype = INSTALL_TYPE_EDITABLE
            elif _looks_like_source_tree(pkg_root):
                itype = INSTALL_TYPE_SOURCE_TREE
            elif approved is not None and fp is not None and approved.content_fingerprint == fp:
                itype = INSTALL_TYPE_OFFICIAL_RELEASE
            elif dist_path is not None:
                itype = INSTALL_TYPE_WHEEL_UNKNOWN
            elif count > 0:
                itype = INSTALL_TYPE_LEGACY_INSTALL
            else:
                itype = INSTALL_TYPE_UNKNOWN

            copies.append(InstallCopy(
                package=pkg,
                package_root=str(pkg_root),
                import_path=str(pkg_root / "__init__.py") if (pkg_root / "__init__.py").is_file() else None,
                root_origin=origin,
                version=ver,
                install_type=itype,
                state="inactive",
                fingerprint=fp,
                fingerprint_scope=scope if fp else None,
                file_count=count,
                mtime_iso=_mtime_of_tree(pkg_root),
                editable_target=editable.get(pkg),
                dist_info_path=dist_path,
                capabilities=caps,
            ))
    copies.sort(key=lambda c: (c.package, c.package_root.lower()))
    return copies
