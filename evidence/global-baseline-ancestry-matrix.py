"""A01 step 1-2: git ancestry / inclusion matrix. Read-only."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = r"C:/hp-testbed"

REFS = {
    "main": "origin/main",
    "v0-2-first-user-release": "origin/feature/v0.2-first-user-release",
    "v0-2-1-deployment-contract": "origin/hotfix/v0.2.1-deployment-contract",
    "e1-generated-context-boundary": "origin/hotfix/e1-generated-context-boundary",
    "embedding-write-reliability": "origin/fix/embedding-write-reliability",
    "embedding-backfill-operator": "origin/fix/embedding-backfill-operator",
    "p0-embedding-integration": "origin/integration/p0-embedding-reliability",
    "runtime-integrity": "origin/feature/runtime-integrity",
    "reliability-recovery-v1": "origin/feature/reliability-recovery-v1",
    "long-observation-index-v1": "origin/feature/long-observation-index-v1",
    "embedding-reliability-recovery": "origin/feature/embedding-reliability-recovery",
    "installer-execution-v2": "feature/installer-execution-v2",
}

PRODUCT_FILES = [
    "src/v3-core/src/v3core/generated_context_contract.py",
    "src/v3-core/src/v3core/llm.py",
    "src/v3-core/src/v3core/e1.py",
    "src/v3-core/src/v3core/embedding.py",
    "src/v3-core/src/v3core/embed_failures.py",
    "src/v3-core/src/v3core/ingest.py",
    "src/v3-core/src/v3core/embed_chunks.py",
    "src/v3-core/src/v3core/observation_chunks.py",
    "src/v3-core/src/v3core/observer.py",
    "src/v3-core/src/v3core/recall_pool.py",
    "src/v3-core/src/v3core/tools/embedding_backfill.py",
    "src/v3-core/schema/qa_embedding_chunks.sql",
    "src/v3-core/schema/observation_embedding_chunks.sql",
    "src/v3-core/schema/upgrade_v0_2.sql",
    "src/v3-core/src/v3core/distribution_cli.py",
    "src/v3-core/src/v3core/tools/health.py",
]


def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def sha(ref: str) -> str:
    return git("rev-parse", ref).strip()


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def merge_base(a: str, b: str) -> str:
    return git("merge-base", a, b).strip()


def blob(ref: str, path: str) -> str | None:
    p = subprocess.run(["git", "-C", REPO, "rev-parse", f"{ref}:{path}"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


print("=== tips ===")
for name, ref in REFS.items():
    print(f"{name:34s} {sha(ref)[:12]}")

print("\n=== ancestry matrix (rows = ancestor?, cols = descendant) ===")
names = list(REFS)
print(" " * 34 + "".join(f"{n[:9]:>11s}" for n in names))
for a in names:
    row = []
    for b in names:
        if a == b:
            row.append("-")
        elif is_ancestor(sha(REFS[a]), sha(REFS[b])):
            row.append("YES")
        else:
            mb = merge_base(sha(REFS[a]), sha(REFS[b]))[:9]
            row.append(f"no({mb[:7]})")
    print(f"{a:34s}" + "".join(f"{c:>11s}" for c in row))

print("\n=== product blob identity per ref (vs recovery tip) ===")
rec = sha(REFS["embedding-reliability-recovery"])
for path in PRODUCT_FILES:
    rb = blob(rec, path)
    marks = []
    for n in names:
        b = blob(sha(REFS[n]), path)
        marks.append("=" if b == rb else ("absent" if b is None else "diff"))
    uniq = len(set(marks))
    print(f"{path.split('/')[-1]:38s} {' '.join(f'{n[:9]}={m}' for n, m in zip(names, marks))}")
