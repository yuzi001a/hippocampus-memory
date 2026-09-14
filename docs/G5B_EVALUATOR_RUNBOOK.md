# G5b Evaluator Reproducibility Runbook

This runbook is for development evaluation only. It does not deploy or alter the V3 runtime.

## 1. Preconditions

- Use a clean evaluator worktree and record the commit:

  ```bash
  git rev-parse HEAD
  git status --porcelain=v1 --untracked-files=all
  ```

- Run from the repository root. The evaluator package is under `src/v3-core/eval`.
- Do not set `V3CORE_CONFIG`, `V3CORE_PG_PASSWORD`, provider keys, or production profile variables for Lane A.
- Never use the reserved production port `5433` (it is a Lane B safety sentinel, not a live endpoint the evaluator ever opens).

## 2. Deterministic Lane A baseline

Lane A is the default and never opens a PG or provider connection:

```bash
export OUT="$(pwd)/outputs/g5b-lane-a-$(date +%Y%m%d-%H%M%S)"
env -u G5B_EVAL_LIVE_PG \
    -u G5B_EVAL_LAB_DSN \
    -u G5B_EVAL_LAB_DSN_OPT_IN \
    -u V3CORE_CONFIG \
    PYTHONPATH=src/v3-core \
    python -m eval.g5b_real_memory_evaluator.run \
      --lane a --out-dir "$OUT"
```

Required artifacts are `results.json`, `summary.json`, and `run.log`. Keep them outside the source tree. Record the exact commit beside the artifacts. A repeat run should have identical scenario outcomes and metrics; latency fields may vary.

Inspect without printing memory payloads:

```bash
python - <<'PY'
import json, os
from pathlib import Path
out = Path(os.environ["OUT"])
summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
print(summary["version"], summary["lane"], summary["metrics"])
PY
```

## 3. Lane B disposable PG only

Lane B is opt-in and must be a separately created disposable database on a separately allocated port such as `55462`. It refuses the reserved production port, reserved database names, reserved production hostnames, and suspicious `PGUSER` inheritance. A missing lab DB fails closed; it never falls back to Lane A.

Only after independently verifying the disposable container/database:

```bash
export G5B_EVAL_LIVE_PG=1
export G5B_EVAL_LAB_DSN='host=127.0.0.1 port=55462 dbname=lab_eval user=lab password=lab'
OUT="$(pwd)/outputs/g5b-lane-b-$(date +%Y%m%d-%H%M%S)"
PYTHONPATH=src/v3-core \
  python -m eval.g5b_real_memory_evaluator.run \
    --lane b --live-pg --dsn "$G5B_EVAL_LAB_DSN" --out-dir "$OUT"
unset G5B_EVAL_LIVE_PG G5B_EVAL_LAB_DSN G5B_EVAL_LAB_DSN_OPT_IN
```

The example credential is synthetic lab data only. Never substitute a production DSN or credential. Lane B setup errors suppress raw DSN/error text.

## 4. Gates before sharing a result

```bash
PYTHONPATH=src/v3-core python -m pytest -q \
  src/v3-core/eval/g5b_real_memory_evaluator/tests/test_g5b_evaluator.py
python -m py_compile \
  src/v3-core/eval/g5b_real_memory_evaluator/lane_a.py \
  src/v3-core/eval/g5b_real_memory_evaluator/lane_b.py \
  src/v3-core/eval/g5b_real_memory_evaluator/run.py
```

The test suite must cover the reserved `5433` guard and Lane B's no-secret error boundary. Do not make the full repository suite a required evaluator gate.

## 5. Cleanup and safety check

Remove only the exact disposable report/cache paths created by the run. Do not use broad filesystem cleanup. Before reporting completion, verify:

```bash
printf 'WORKTREE_STATUS='; git status --porcelain=v1 --untracked-files=all | wc -l
printf 'PROD_CONFIG_MTIME='; stat -c '%y' "$HOME/.v3-core/profiles/default/config.yaml" 2>/dev/null || true
```

A reproducibility report must state: lane, exact commit, scenario count, artifact path, whether Lane B was used, test result, and confirmation that production config/PG/public repo were untouched.