"""Reproduce product-ci.yml verbatim: every focused pytest step, with the CI's own env contract."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(r"C:/hp-testbed")
WF = REPO / ".github" / "workflows" / "product-ci.yml"
PY = r"C:/Users/servi/workspace/backups/a01-env-cand/Scripts/python.exe"

text = WF.read_text(encoding="utf-8")
# split into steps on the "- name:" boundaries
steps = re.split(r"\n      - name: ", text)[1:]
results = []

for raw in steps:
    name = raw.split("\n", 1)[0].strip()
    if "python -m pytest" not in raw:
        continue
    wd = re.search(r"working-directory:\s*(\S+)", raw)
    workdir = REPO / (wd.group(1) if wd else ".")
    # declared env: NAME: "value" (values are hard-empty in this workflow)
    env = dict(re.findall(r"^\s{10}([A-Z0-9_]+):\s*\"([^\"]*)\"", raw, re.M))
    # pytest target files
    files = re.findall(r"((?:tests|eval)/[A-Za-z0-9_./]+\.py)", raw)
    cmd = [PY, "-m", "pytest", "-v", "--no-header", "-p", "no:cacheprovider", *files]
    tmp = str(Path.home() / "AppData" / "Local" / "Temp")
    run_env = {"PYTHONPATH": "src", "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
               "PATH": os.environ.get("PATH", r"C:\Windows\System32;C:\Windows"),
               "TEMP": tmp, "TMP": tmp,
               # pathlib.expanduser() needs a resolvable home inside the harness environment
               "USERPROFILE": str(Path.home()),
               "HOMEDRIVE": os.path.splitdrive(str(Path.home()))[0],
               "HOMEPATH": os.path.splitdrive(str(Path.home()))[1],
               "APPDATA": os.environ.get("APPDATA", ""),
               "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", "")}
    run_env.update(env)
    r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, env=run_env,
                       encoding="utf-8", errors="replace")
    tail = [ln for ln in (r.stdout or "").splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    results.append((name, len(files) or len(re.findall(r"(eval/|scripts/)", raw)), r.returncode,
                    tail[-1] if tail else (r.stdout or r.stderr)[-160:].strip()))

print()
for name, nfiles, rc, tail in results:
    print(f"{'PASS' if rc == 0 else 'FAIL'}  {name} ({nfiles} files) exit={rc}  {tail}")
verdict = "PASS" if results and all(rc == 0 for _, _, rc, _ in results) else "FAIL"
print("\nREQUIRED_CI_LOCAL_REPRODUCTION =", verdict, f"({len(results)} pytest steps)")

(REPO / "evidence" / "global-baseline-ci-local-reproduction.txt").write_text(
    "workflow: product-ci.yml (verbatim focused-test steps, hard-empty credential contract)\n" +
    "\n".join(f"{'PASS' if rc == 0 else 'FAIL'} {n} exit={rc} {t}" for n, _, rc, t in results) +
    f"\nREQUIRED_CI_LOCAL_REPRODUCTION = {verdict} ({len(results)} steps)\n", encoding="utf-8")
