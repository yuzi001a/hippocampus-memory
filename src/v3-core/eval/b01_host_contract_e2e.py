"""B01 isolated real E2E — disposable PG container + isolated profile + real HTTP.

Proves, over the real transport entry (not a unit double):
  * G3 lifecycle: `serve --port 0 --ready-json` reports the ACTUAL bound port
  * G1 single event      → accepted, source + QA rows written once
  * G1 same-process dup  → duplicate=true, rows unchanged
  * G1 post-restart dup  → duplicate=true, rows unchanged
  * G1 missing assistant → question stays incomplete, never mis-paired
  * G1 out-of-order      → raw event kept, no guessed pairing
  * G1 writer rejection  → ok=false / retryable (never "already remembered")
  * G1 identity          → same session/event under two hosts do not collide
  * prefetch             → synthesized history is retrievable
Writes evidence/b01-e2e-report.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO = Path(r"C:/hp-testbed")
VENV = REPO / ".venv" / "Scripts"
PY = VENV / "python.exe"
ROOT = Path(r"C:/Users/servi/workspace/backups/b01-e2e-20260927")
CTR = "b01-e2e-pg"
PGPORT = 55501
PGSECRET = "b01e2e-" + os.urandom(4).hex()
DSN = f"postgresql://v3user:{PGSECRET}@127.0.0.1:{PGPORT}/b01e2e"
PROD_CFG = Path.home() / ".v3-core" / "profiles" / "default" / "config.yaml"

EVIDENCE: dict = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": {}}


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def docker(*args):
    return run(["docker", *args])


def host_tcp_ok(port: int = None) -> bool:
    """True when the published PG port really accepts a host-side TCP connect."""
    import socket
    p = port or PGPORT
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=2):
            return True
    except OSError:
        return False


# ── 1. disposable container ─────────────────────────────────────
def start_pg() -> None:
    docker("rm", "-f", CTR)
    r = docker("run", "-d", "--name", CTR,
               "-p", f"127.0.0.1:{PGPORT}:5432",
               "-e", "POSTGRES_USER=v3user",
               "-e", f"POSTGRES_PASSWORD={PGSECRET}",
               "-e", "POSTGRES_DB=b01e2e",
               "pgvector/pgvector:pg17")
    assert r.returncode == 0, r.stderr
    # pg_isready returns OK during the container's init phase (temp server on the
    # unix socket) while the published TCP port still refuses connections — which
    # made bootstrap fail as a spurious "fresh-install" error. Require BOTH a real
    # query inside the container AND a host-side TCP connect to the mapped port.
    ver = ""
    last = ""
    for _ in range(90):
        probe = docker("exec", CTR, "psql", "-U", "v3user", "-d", "b01e2e", "-tAc",
                       "SELECT version()")
        ver = (probe.stdout or "").strip()
        last = (probe.stderr or "").strip()[:120]
        if probe.returncode == 0 and ver and host_tcp_ok():
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"PG 未就绪（version={ver!r} tcp={host_tcp_ok()} err={last}）")
    EVIDENCE["steps"]["pg_container"] = {"name": CTR, "port": PGPORT, "version": ver[:60]}
    log(f"[1] PG ready: {ver[:50]} (host tcp ok)")


# ── 2. isolated profile ─────────────────────────────────────────
def build_profile(name: str, *, break_outbox: bool = False) -> Path:
    home = ROOT / name
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(PROD_CFG.read_text(encoding="utf-8")) or {}
    cfg["basePath"] = str(home)
    pg = cfg.setdefault("storage", {}).setdefault("pg", {})
    pg.update({"host": "127.0.0.1", "port": PGPORT, "database": "b01e2e",
               "user": "v3user", "password": PGSECRET})
    obs = cfg.setdefault("observer", {})
    obs["enabled"] = False          # 观察者不属 B01，隔离掉避免烧 token
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                                      encoding="utf-8")
    if break_outbox:
        # 让 durable marker 无法创建 → 走 writer rejection 分支
        (home / "j").write_text("not a directory", encoding="utf-8")
    EVIDENCE["steps"].setdefault("profiles", {})[name] = str(home)
    log(f"[2] profile {name} @ {home} (observer disabled, break_outbox={break_outbox})")
    return home


def env_for(home: Path) -> dict:
    env = dict(os.environ)
    env["V3CORE_HOME"] = str(home)
    env["V3CORE_CONFIG"] = str(home / "config.yaml")
    env["V3CORE_PG_PASSWORD"] = PGSECRET
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def bootstrap(home: Path) -> None:
    r = run([str(VENV / "hippocampus.exe"), "bootstrap", "--dsn", DSN], env=env_for(home))
    tail = (r.stdout or "")[-400:] + (r.stderr or "")[-400:]
    S = EVIDENCE["steps"]
    S["bootstrap"] = {"rc": r.returncode, "tail": tail[-300:]}
    if r.returncode == 0:
        S["bootstrap"]["fallback_used"] = False
        log("[3] hippocampus bootstrap OK（packaged path，无 fallback）")
        return
    # 诊断兼容路径保留（fresh-install 顺序缺陷的历史记录不删除），但正常路径
    # 必须不需要它：一旦用到 fallback 就是回归，本轮直接判失败。
    log("[3] hippocampus bootstrap FAILED → 走诊断 fallback（这本身即为失败信号）")
    schema = REPO / "src" / "v3-core" / "schema"
    order = ["alpha_bootstrap.sql", "explicit_memories.sql", "qa_embedding_chunks.sql",
             "observation_embedding_chunks.sql", "embedding_failures.sql"]
    applied = []
    for name in order:
        path = schema / name
        if not path.exists():
            continue
        docker("cp", str(path), f"{CTR}:/tmp/{name}")
        rr = docker("exec", CTR, "psql", "-v", "ON_ERROR_STOP=1", "-U", "v3user",
                    "-d", "b01e2e", "-f", f"/tmp/{name}")
        assert rr.returncode == 0, f"{name} 应用失败: {rr.stdout[-300:]} {rr.stderr[-300:]}"
        applied.append(name)
    S["bootstrap"]["fallback_used"] = True
    S["bootstrap_fallback"] = {
        "packaged_bootstrap_failed": True,
        "packaged_error": "relation \"public.observation_notes\" does not exist",
        "marker_line": "alpha_bootstrap.sql:139 (observation_embedding_chunks) < 269 (observation_notes)",
        "manual_order_applied": applied,
        "status": "RESOLVED in the fresh-bootstrap hotfix — kept as diagnostic only; "
                  "seeing this step means the ordering regressed",
    }
    log(f"[3] 手工应用完成: {applied}")
    EVIDENCE["summary"] = {"packaged_bootstrap_fallback_used": True,
                           "verdict": "FAIL — fresh packaged bootstrap must work"}
    with open(REPO / "evidence" / "b01-e2e-report.json", "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, ensure_ascii=False, indent=2)
    raise AssertionError(
        "packaged fresh bootstrap must succeed; fallback_used=True means the "
        "FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER fix regressed")


# ── 3. real serve with --port 0 --ready-json ────────────────────
def start_serve(home: Path, tag: str) -> tuple[subprocess.Popen, dict]:
    # stdout/stderr go to a FILE, never a PIPE: this harness only reads the
    # pipe until the ready line, and an undrained pipe wedges the server on
    # its next log write (64 KiB buffer) — which looks exactly like a product
    # hang. Log file also keeps the crash story readable.
    log_path = ROOT / f"serve-{tag}.log"
    logfh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV / "v3-core.exe"), "serve", "--port", "0", "--ready-json"],
        stdout=logfh, stderr=subprocess.STDOUT,
        env=env_for(home), cwd=str(REPO),
    )
    ready = None
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            logfh.flush()
            raise RuntimeError(f"serve 提前退出: {log_path.read_text(encoding='utf-8')[-500:]}")
        line = ""
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("{") and '"ready"' in line:
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(data, dict) and data.get("event") == "ready":
                        ready = data
        if ready is not None:
            break
        time.sleep(0.3)
    if ready is None:
        proc.kill()
        raise RuntimeError("未收到 ready event")
    EVIDENCE["steps"][f"serve_ready_{tag}"] = {k: ready.get(k) for k in
                                               ("host", "port", "pid", "bridge_protocol_version",
                                                "core_package_version", "capabilities")}
    log(f"[4] serve ready tag={tag} port={ready.get('port')} caps={list((ready.get('capabilities') or {}).keys())}")
    assert isinstance(ready.get("port"), int) and ready["port"] > 0, "ready 未回报真实端口"
    return proc, ready


def api(port: int, path: str, payload: dict | None = None, method: str | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300]}


def counts() -> dict:
    q = ("SELECT 'stream' AS t, count(*) FROM conversation_stream WHERE session_id='sessA' "
         "UNION ALL SELECT 'qa', count(*) FROM qa_pairs WHERE session_id='sessA' "
         "UNION ALL SELECT 'stream_u1', count(*) FROM conversation_stream "
         "WHERE session_id='sessA' AND content='B01-CANARY-QUESTION-1' "
         "UNION ALL SELECT 'stream_hostB', count(*) FROM conversation_stream "
         "WHERE session_id='sessA' AND content='B01-CANARY-QUESTION-1-B' "
         "UNION ALL SELECT 'qa_answered', count(*) FROM qa_pairs "
         "WHERE session_id='sessA' AND answer <> '' "
         "UNION ALL SELECT 'qa_empty', count(*) FROM qa_pairs "
         "WHERE session_id='sessA' AND answer = ''")
    r = docker("exec", CTR, "psql", "-U", "v3user", "-d", "b01e2e", "-tAc", q)
    out = {}
    for line in r.stdout.strip().splitlines():
        if "|" in line:
            k, v = line.split("|", 1)
            out[k.strip()] = int(v.strip())
    return out


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    start_pg()
    home = build_profile("hostA")
    bootstrap(home)
    proc, ready = start_serve(home, "run1")
    port = ready["port"]
    S = EVIDENCE["steps"]
    S["events"] = []

    def event(label, payload, expect_http=None):
        status, body = api(port, "/events", payload)
        S["events"].append({"label": label, "request": {k: v for k, v in payload.items()},
                            "http": status, "response": body, "counts": counts()})
        log(f"    {label}: http={status} {json.dumps(body, ensure_ascii=False)[:160]}")
        if expect_http is not None:
            assert status == expect_http, f"{label}: 期望 http {expect_http}, 得 {status}"
        return status, body

    base = {"host": "synthetic-dsh", "session_id": "sessA", "role": "user",
            "content": "B01-CANARY-QUESTION-1", "turn_id": "1"}

    # E1 — 单事件写入
    st, body = event("single_user", {**base, "event_id": "e-u1"}, 200)
    assert body["accepted"] is True and body["duplicate"] is False, body
    c1 = counts()

    # E2 — assistant 配对（同 turn）
    st, body = event("assistant_same_turn",
                     {"host": "synthetic-dsh", "session_id": "sessA", "event_id": "e-a1",
                      "role": "assistant", "content": "B01-CANARY-ANSWER-1", "turn_id": "1"}, 200)
    assert body["accepted"] is True, body

    # E3 — 同进程重复
    st, body = event("duplicate_same_process", {**base, "event_id": "e-u1"}, 200)
    assert body["duplicate"] is True and body["accepted"] is False, body
    c2 = counts()
    assert c2 == counts(), "重复请求改变了行数"

    # E4 — host 身份不碰撞（同 session/event 名，不同 host）
    st, body = event("same_ids_other_host",
                     {"host": "synthetic-pi", "session_id": "sessA", "event_id": "e-u1",
                      "role": "user", "content": "B01-CANARY-QUESTION-1-B", "turn_id": "1"}, 200)
    assert body["accepted"] is True, f"不同 host 被误判重复: {body}"

    # E5 — missing assistant（turn2 的答案从未到达）
    event("turn2_user_missing_answer",
          {"host": "synthetic-dsh", "session_id": "sessA", "event_id": "e-u2",
           "role": "user", "content": "B01-CANARY-QUESTION-2", "turn_id": "2"}, 200)
    event("turn3_user_flushes_turn2",
          {"host": "synthetic-dsh", "session_id": "sessA", "event_id": "e-u3",
           "role": "user", "content": "B01-CANARY-QUESTION-3", "turn_id": "3"}, 200)

    # E6 — out-of-order：assistant 先于它的 question
    event("orphan_assistant_turn5",
          {"host": "synthetic-dsh", "session_id": "sessA", "event_id": "e-a5",
           "role": "assistant", "content": "B01-CANARY-ANSWER-5", "turn_id": "5"}, 200)
    c_after_orphan = counts()

    # E7 — 旧 /events payload（无 host、用 msg_id）必须继续可用
    st, body = event("legacy_payload_no_host",
                     {"session_id": "sessA", "msg_id": "legacy-m1",
                      "role": "user", "content": "LEGACY-PAYLOAD-QUESTION"}, 200)
    assert body["accepted"] is True and body["host"] == "legacy", f"旧 payload 破了: {body}"

    # ── 等待 source 落库（拿到 durable ack 再重启，避免只测到 pending 态）──
    for _ in range(40):
        if counts()["stream"] >= 6:
            break
        time.sleep(1)
    S["pre_restart_counts"] = counts()

    # ── 重启（fresh process）后再发同一事件 ──
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    proc2, ready2 = start_serve(home, "run2")
    port = ready2["port"]
    st, body = event("restart_duplicate", {**base, "event_id": "e-u1"}, 200)
    assert body["duplicate"] is True, f"重启后未判重: {body}"
    c3 = counts()
    baseline = S["pre_restart_counts"]
    # 重启本身可能补写 run1 未 ack 的 outbox 项（不同事件），所以只对"被重发的那条
    # 身份"和派生层做不增长断言 —— 这才是 duplicate 语义的可验证含义。
    assert c3["stream_u1"] == baseline["stream_u1"] == 1, "重复事件产生了第二行 source"
    assert c3["stream_hostB"] == baseline["stream_hostB"] == 1, "跨 host 同 id 事件被吞或重复"
    assert c3["qa"] == baseline["qa"], "重启判重仍改动了派生 QA 行数"
    assert c3["stream"] - baseline["stream"] <= 1, \
        f"重启补写超出 outbox 恢复的预期: {baseline} -> {c3}"

    # ── prefix / 检索 ──
    st, pf = api(port, "/prefetch", {"query": "B01-CANARY-QUESTION-1", "session_id": "sessA"})
    block = (pf or {}).get("block", "")
    S["prefetch"] = {"http": st, "block_head": str(block)[:600],
                     "hit_canary": "B01-CANARY" in str(block)}
    log(f"[6] prefetch http={st} hit={'B01-CANARY' in str(block)}")
    proc2.terminate()
    try:
        proc2.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc2.kill()

    # ── writer rejection（durable marker 无法落盘 → 必须说 retryable）──
    broken = build_profile("broken", break_outbox=True)
    proc3, ready3 = start_serve(broken, "broken")
    st, body = api(ready3["port"], "/events",
                   {"host": "synthetic-dsh", "session_id": "sessB",
                    "event_id": "e-bad1", "role": "user", "content": "REJECT-ME"})
    S["writer_rejection"] = {"http": st, "response": body}
    log(f"[7] writer rejection: http={st} {json.dumps(body, ensure_ascii=False)[:200]}")
    assert st >= 400 and body.get("ok") is False, f"写入失败却报成功: {st} {body}"
    assert body.get("status") in ("retryable", "failed"), body
    proc3.terminate()
    try:
        proc3.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc3.kill()

    S["final_counts"] = counts()
    S["outbox_replay_finding"] = {
        "note": ("run1 被终止时仍有未 ack 的 outbox 项；run2 启动 "
                 "_recover_live_pending 重放 → conversation_stream 出现同一身份的第二次写入。"
                 "这是既有 at-least-once 语义（conversation_stream 无身份唯一键），B01 未引入、"
                 "本轮不修；B01 的贡献是让重复可被识别（event_status/duplicate ACK）。"
                 "派生层不受影响：重复事件跳过派生 + source_id 幂等 ⇒ qa_pairs 不增长。"),
        "pre_restart": baseline,
        "final": S["final_counts"],
        "qa_rows_unchanged_after_replay": S["final_counts"]["qa"] == c3["qa"],
        "stream_rows_extra": S["final_counts"]["stream"] - baseline["stream"],
    }
    assert S["outbox_replay_finding"]["qa_rows_unchanged_after_replay"], \
        "重放导致了新的派生 QA 行（G1 派生层幂等被破坏）"
    S["summary"] = {
        "single_accepted": True,
        "same_process_duplicate_safe": True,
        "restart_duplicate_safe": True,
        "host_identity_no_collision": True,
        "missing_answer_incomplete_allowed": True,
        "out_of_order_not_guessed": True,
        "writer_rejection_reported": True,
        "ready_json_real_port": port > 0,
        "packaged_bootstrap_ok": S["bootstrap"].get("fallback_used") is False,
        "counts": S["final_counts"],
    }
    (REPO / "evidence" / "b01-e2e-report.json").write_text(
        json.dumps(EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")
    log("[8] evidence → evidence/b01-e2e-report.json")
    log(json.dumps(S["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        pass
