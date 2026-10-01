"""F2 harness contract — the guards and cleanup logic, unit-tested offline.

What this file covers, and what it deliberately does not:

* COVERED — every refusal the harness relies on, exercised with fakes only:
  the machine gate, the path/data-root isolation gate, the credential gate,
  the PG allowlist, the process-identity verification, the owned-child
  cleanup and leftover abort, the fail-closed teardown (a cleanup exception
  is recorded, rejects the run, and PRESERVES the generated config), the
  ACCEPTANCE SEAL itself (full matrix, mandatory guard phases, run_error,
  child agreement, residual pids, provider count, sentinel, config
  lifecycle, ``--only`` subsets labelled PARTIAL), the production-sentinel
  diff, the pre/post commit barrier classification, the child self-report
  assertions, the provider-marker scan, the child environment, and the
  fault-injecting TCP proxies (against a throwaway loopback echo server,
  never v3core).
* NOT COVERED — W0-W4 themselves. Those need a disposable PostgreSQL and a
  real serve child, so they are the harness's job, not this file's. No test
  here opens a PG connection, and the v3core pytest domain hard-blocks
  ``psycopg2.connect`` anyway.

Isolation of this file: the only filesystem the guards touch is a
``tmp_path`` sandbox. ``Path.home()`` is redirected by the session conftest,
and the production-sentinel helpers are exercised against an explicit
``home=tmp_path`` so no real profile is ever read — let alone fingerprinted.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# Make ``v3core`` and this package importable from a bare checkout.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
_REPO_ROOT = os.path.normpath(os.path.join(_PKG_ROOT, "..", ".."))
for _p in (os.path.join(_PKG_ROOT, "src"), _PKG_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval import f2_source_idempotency_e2e as H  # noqa: E402

# The harness's destructive half is Windows-only by contract (the machine gate
# admits exactly one Windows test laptop), so the Windows-process and
# Windows-command surfaces are asserted on Windows and skipped elsewhere.
# Everything in this file that is platform independent — the machine gate
# itself, path isolation, the credential gate, the PG allowlist, process
# IDENTITY verification against fakes, leftover abort, the sentinel, the
# barriers, the proxies, the provider scan — runs on BOTH, so the Linux CI
# gate still covers the isolation logic rather than importing it untested.
WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows process/command surface; the harness only runs on the "
           "dedicated Windows test laptop")


# ══════════════════════════════════════════════════════════════════════════
# fakes
# ══════════════════════════════════════════════════════════════════════════
class FakeProc:
    """Minimal ``subprocess.Popen`` stand-in."""

    def __init__(self, pid: int = 4321, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.stdout = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True


def _identity(pid: int, created: str = "2026-10-02T01:00:00.0000000Z",
              cmdline: str = "python -m eval.f2_source_idempotency_e2e child-serve") -> dict:
    return {"pid": int(pid), "ppid": 1, "created": created, "cmdline": cmdline}


class FakeTable:
    """``{pid: identity}`` backing a fake ``lookup``."""

    def __init__(self, *identities: dict) -> None:
        self.rows = {int(i["pid"]): dict(i) for i in identities}

    def lookup(self, pid: int) -> dict | None:
        return self.rows.get(int(pid))

    def kill(self, pid: int) -> dict:
        self.rows.pop(int(pid), None)
        return {"rc": 0, "out": f"SUCCESS: killed {pid}", "err": ""}


class _EchoServer:
    """Throwaway loopback HTTP-ish server: echoes one request, then answers.

    Used only to give :class:`H.DirectionProxy` something real to sit in
    front of. It never imports v3core and never leaves loopback.
    """

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = int(self.sock.getsockname()[1])
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self.sock.settimeout(10)
        try:
            client, _ = self.sock.accept()
        except OSError:
            return
        try:
            client.settimeout(5)
            data = client.recv(65536)
            if data:
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                               b"Connection: close\r\n\r\nok")
            client.close()
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════
# tracked-path contract — no reliance on the ignored scripts/ tree
# ══════════════════════════════════════════════════════════════════════════
def test_harness_lives_in_a_tracked_location():
    assert H.EVAL_DIR == Path(H.__file__).resolve().parent
    assert H.EVAL_DIR.name == "eval"
    assert H.EVAL_DIR.is_relative_to(H.PKG_DIR)
    assert H.PKG_DIR.name == "v3-core"
    assert (H.REPO_ROOT / "src" / "v3-core" / "eval").is_dir()


def test_harness_has_no_dependency_on_the_ignored_scripts_tree():
    """Everything the harness needs at run time must be tracked.

    ``scripts/`` is in .gitignore, so a harness that imported from it would
    be unrunnable for anyone who cloned the repo. Asserted on the module
    text: no ``scripts`` import, no ``sys.path`` surgery, no exec of an
    external helper.
    """
    text = Path(H.__file__).read_text(encoding="utf-8")
    body = text.split('"""', 2)[-1]  # drop the module docstring
    for banned in ("sys.path.insert", "import scripts", "from scripts",
                   "exec(", "eval(", "__import__"):
        assert banned not in body, f"harness must not use {banned!r}"
    # The child roles are this module, reached through -m.
    assert "-m" in H.child_argv("child-serve")
    assert "eval.f2_source_idempotency_e2e" in H.child_argv("child-serve")


def test_child_argv_is_interpreter_relative_and_role_tagged():
    argv = H.child_argv("child-client", "--mode", "deliver", "--port", "1")
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "eval.f2_source_idempotency_e2e"]
    assert argv[3] == "child-client"
    assert argv[4:] == ["--mode", "deliver", "--port", "1"]


def test_cli_exposes_make_and_both_child_roles():
    parser = H.build_parser()
    args = parser.parse_args(["make", "--root", "C:/tmp/f2", "--pg-port", "55432"])
    assert args.command == "make" and args.pg_port == 55432
    # A Windows path with spaces must survive the CLI unquoted.
    spaced = parser.parse_args(["make", "--root", "C:/hp-testbed/f2 overnight/run",
                                "--pg-port", "55432"])
    assert spaced.root == "C:/hp-testbed/f2 overnight/run"
    serve = parser.parse_args(["child-serve", "--port", "0"])
    assert serve.command == "child-serve" and serve.barrier_event == ""
    client = parser.parse_args(["child-client", "--mode", "no-ack", "--port", "9",
                                "--payload", "{}"])
    assert client.command == "child-client" and client.mode == "no-ack"


# ══════════════════════════════════════════════════════════════════════════
# I1 — machine gate
# ══════════════════════════════════════════════════════════════════════════
def test_machine_gate_accepts_only_the_dedicated_test_laptop():
    record = H.assert_test_host("DESKTOP-EQP3OBU")
    assert record["verdict"] == "PASS"
    assert record["hostname"] == "DESKTOP-EQP3OBU"
    assert "DESKTOP-MU2RQIU" in record["forbidden_hosts"]


def test_machine_gate_refuses_the_production_win_nas_by_name():
    with pytest.raises(H.HostGateError) as excinfo:
        H.assert_test_host("DESKTOP-MU2RQIU")
    message = str(excinfo.value)
    assert "DESKTOP-MU2RQIU" in message
    assert "PRODUCTION" in message
    assert "DESKTOP-EQP3OBU" in message


@pytest.mark.parametrize("hostname", [
    "WORKSTATION-1", "laptop", "DESKTOP-EQP3OB", "DESKTOP-EQP3OBU-2",
    "desktop-eqp3obu ",  # trailing space is not silently trimmed into a pass
])
def test_machine_gate_refuses_every_other_host(hostname):
    with pytest.raises(H.HostGateError):
        H.assert_test_host(hostname)


@pytest.mark.parametrize("blank", [None, ""])
def test_a_blank_hostname_falls_back_to_the_live_host(monkeypatch, blank):
    """I1: a blank is not a pass, and not a refusal either — it is the LIVE host.

    ``assert_test_host`` resolves ``hostname or current_hostname()``, so a
    caller that supplies nothing is asking about the machine it is running on.
    That has to hold on EVERY host: the dedicated laptop accepts, and any
    other host refuses with the real name in the message. Asserting a fixed
    outcome here instead would bake in the assumption that the suite is never
    run on the test laptop — which is exactly where this harness is meant to
    run, and why the assumption failed on y400.
    """
    # A live host that is NOT the laptop: the blank must refuse, by name.
    monkeypatch.setattr(H, "current_hostname", lambda: "WORKSTATION-1")
    with pytest.raises(H.HostGateError) as excinfo:
        H.assert_test_host(blank)
    assert "WORKSTATION-1" in str(excinfo.value)
    # The live host that IS the laptop: the same blank call must accept.
    monkeypatch.setattr(H, "current_hostname", lambda: H.TEST_LAPTOP_HOSTNAME)
    record = H.assert_test_host(blank)
    assert record["verdict"] == "PASS"
    assert record["hostname"] == H.TEST_LAPTOP_HOSTNAME


def test_machine_gate_defaults_to_the_live_hostname(monkeypatch):
    # The gate must consult the real host when not told which host — and it
    # must consult it, not assume an outcome. On the dedicated laptop the live
    # default is a PASS; everywhere else it is a refusal. The point under test
    # is the LOOKUP, so it is asserted with a pinned live host and then
    # cross-checked against the real one.
    monkeypatch.setattr(H, "current_hostname", lambda: "WORKSTATION-1")
    with pytest.raises(H.HostGateError) as excinfo:
        H.assert_test_host()          # no argument at all -> live host
    assert "WORKSTATION-1" in str(excinfo.value)

    # On the real host the outcome is whatever the real host is, and it is the
    # same outcome as naming that host explicitly. No environment is baked in.
    monkeypatch.undo()          # the real socket.gethostname() again
    live = H.current_hostname()
    assert live
    try:
        expected = H.assert_test_host(live)
    except H.HostGateError as exc:
        with pytest.raises(H.HostGateError) as from_default:
            H.assert_test_host()
        assert str(from_default.value) == str(exc)
    else:
        assert H.assert_test_host() == expected


# ══════════════════════════════════════════════════════════════════════════
# I2/I5 — path isolation
# ══════════════════════════════════════════════════════════════════════════
def test_is_under_is_case_insensitive_and_handles_siblings(tmp_path):
    root = tmp_path / "run"
    assert H.is_under(root, root)
    assert H.is_under(root / "a" / "b", root)
    # A sibling that merely shares a prefix is NOT under the root.
    assert not H.is_under(str(root) + "-other", root)
    assert not H.is_under(tmp_path, root)
    if os.name == "nt":  # case folding is a Windows-only property
        assert H.is_under(str(root / "a").upper(), root)


def test_assert_all_under_rejects_a_foreign_data_root(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    good = H.assert_all_under([("data_dir", root / "profile")], root, "child")
    assert good["all_under_root"] is True
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_all_under([("data_dir", tmp_path / "elsewhere")], root, "child")
    assert "data_dir" in str(excinfo.value)
    assert "OUTSIDE" in str(excinfo.value)


def test_child_provenance_rejects_a_config_outside_the_root(tmp_path):
    root = tmp_path / "run"
    (root / "profile").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    good = {
        "refused": False, "test_mode": "1", "config_path_env": str(root / "profile" / "config.yaml"),
        "config_path_resolved": str(root / "profile" / "config.yaml"),
        "declared_base_path": str(root / "profile"),
        "data_dir": str(root / "profile"), "core_base_path": str(root / "profile"),
        "topic_sqlite_path": str(root / "profile" / "v3_topic.db"),
        "pending_dir": str(root / "profile" / "j" / "pending_live_buffer"),
        "accepted_dir": str(root / "profile" / "j" / "accepted_live_buffer"),
        "pg": {"host": "127.0.0.1", "port": 55432, "database": "f2e2e",
               "user": "v3user", "password_present": True},
    }
    record = H.assert_child_provenance(good, root, "f2e2e", 55432, "v3user")
    assert record["isolated"] is True
    # A resolved data root outside the isolated root is the production leak
    # this whole mechanism exists to catch.
    leaked = dict(good, data_dir=str(other), core_base_path=str(other))
    with pytest.raises(H.IsolationError):
        H.assert_child_provenance(leaked, root, "f2e2e", 55432, "v3user")


def test_child_provenance_rejects_a_foreign_database_or_missing_test_mode(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    prov = {"refused": False, "test_mode": "1",
            "config_path_env": str(root / "config.yaml"),
            "config_path_resolved": str(root / "config.yaml"),
            "data_dir": str(root), "core_base_path": str(root),
            "topic_sqlite_path": str(root / "v3_topic.db"),
            "pending_dir": str(root / "j" / "p"), "accepted_dir": str(root / "j" / "a"),
            "pg": {"host": "127.0.0.1", "port": 55432, "database": "v3embeddings",
                   "user": "v3user", "password_present": True}}
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_child_provenance(prov, root, "f2e2e", 55432, "v3user")
    assert "database" in str(excinfo.value)

    without_flag = dict(prov, test_mode="", pg=dict(prov["pg"], database="f2e2e"))
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_child_provenance(without_flag, root, "f2e2e", 55432, "v3user")
    assert "V3CORE_TEST_MODE" in str(excinfo.value)


def test_child_provenance_surfaces_a_test_mode_refusal(tmp_path):
    refused = {"refused": True, "reason": "V3CORE_TEST_MODE fail-closed: ...",
               "refusal_prefix_ok": True, "config_path_env": "",
               "config_path_resolved": "", "data_dir": ""}
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_child_provenance(refused, tmp_path, "f2e2e", 55432, "v3user")
    assert "refused to boot" in str(excinfo.value)


def test_refusal_never_escapes_as_an_unclassifiable_crash(monkeypatch, tmp_path):
    """REGRESSION: the T1 refusal is a SystemExit, not an Exception.

    ``config._find_config`` / ``_resolve_data_dir`` refuse by raising a
    ``SystemExit`` subclass, and the T1 sequence (delete the config, restart
    the child) fires exactly there. If the self-report did not catch it, the
    child would die with a bare exit code and the harness would record a
    crash instead of a refusal — losing the one distinction T1 exists to
    make. Patched here rather than simulated so the test runs everywhere.
    """
    class Refusal(SystemExit):
        pass

    import v3core.config as v3config
    monkeypatch.setattr(v3config, "_find_config",
                        lambda *a, **k: (_ for _ in ()).throw(
                            Refusal(f"{H.TEST_MODE_REFUSAL_PREFIX}: gone")))

    prov = H.collect_child_provenance()
    assert prov["refused"] is True
    assert prov["refusal_prefix_ok"] is True
    assert prov["stage"] == "resolve_config"
    # A refusal is still a report, so it must be classifiable as one.
    with pytest.raises(H.IsolationError):
        H.assert_child_provenance(prov, tmp_path, "f2e2e", 55432, "v3user")


def test_provenance_reports_a_non_refusal_base_exception_as_refused(monkeypatch, tmp_path):
    """A KeyboardInterrupt-shaped failure must not escape either."""
    import v3core.config as v3config
    monkeypatch.setattr(v3config, "_find_config",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    prov = H.collect_child_provenance()
    assert prov["refused"] is True
    assert prov["refusal_prefix_ok"] is False
    assert "KeyboardInterrupt" in prov["reason"]


def test_all_children_must_resolve_the_same_root(tmp_path):
    root = tmp_path / "run"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)

    def rec(role: str, data_dir: Path) -> dict:
        return {"role": role, "pid": 1, "provenance": {
            "refused": False, "test_mode": "1",
            "config_path_env": str(data_dir / "config.yaml"),
            "config_path_resolved": str(data_dir / "config.yaml"),
            "data_dir": str(data_dir), "core_base_path": str(data_dir),
            "topic_sqlite_path": str(data_dir / "v3_topic.db"),
            "pending_dir": str(data_dir / "j" / "p"),
            "accepted_dir": str(data_dir / "j" / "a"),
            "pg": {"host": "127.0.0.1", "port": 55432, "database": "f2e2e",
                   "user": "v3user", "password_present": True}}}

    agreed = H.assert_all_children_agree([rec("a", root / "a"), rec("b", root / "a")],
                                        root, "f2e2e", 55432, "v3user")
    assert agreed["agreed"] is True
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_all_children_agree([rec("a", root / "a"), rec("b", root / "b")],
                                    root, "f2e2e", 55432, "v3user")
    assert "DIFFERENT data roots" in str(excinfo.value)


# ══════════════════════════════════════════════════════════════════════════
# I3 — credentials
# ══════════════════════════════════════════════════════════════════════════
def test_generated_profile_has_no_credential_and_names_the_isolated_root(tmp_path):
    base = tmp_path / "run" / "profile"
    path = H.write_profile(base, "f2e2e", 55432, "v3user")
    assert path.is_file()
    record = H.assert_no_credentials(path, secret="")
    assert record["all_credential_fields_empty"] is True
    assert record["pg_password_in_file"] is False
    # password_present is the env channel, never the file.
    assert "password" not in path.read_text(encoding="utf-8").lower().split(
        "embed")[0].split("pg:")[1]


@pytest.mark.parametrize("field,value", [
    (("storage", "pg", "password"), "hunter2"),
    (("storage", "embed", "apiKey"), "sk-live"),
    (("storage", "embed", "endpoint"), "https://api.example.com/v1"),
    (("llm", "api_key"), "sk-live"),
    (("llm", "base_url"), "https://llm.example.com"),
])
def test_credential_gate_fails_the_run_on_any_non_empty_credential(tmp_path, field, value):
    import yaml
    base = tmp_path / "run" / "profile"
    base.mkdir(parents=True)
    raw = {"basePath": str(base), "mode": "cloud",
           "storage": {"pg": {"host": "127.0.0.1", "port": 55432,
                              "database": "f2e2e", "user": "v3user"},
                       "embed": {"endpoint": "", "model": "", "apiKey": "",
                                 "api_key": "", "proxy": ""},
                       "rerank": {"endpoint": "", "model": "", "proxy": "",
                                  "apiKey": "", "api_key": ""}},
           "llm": {"provider": "", "model": "", "api_key": "", "base_url": ""}}
    node = raw
    for key in field[:-1]:
        node = node[key]
    node[field[-1]] = value
    path = base / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_no_credentials(path, secret="")
    assert ".".join(field) in str(excinfo.value)


def test_credential_gate_also_catches_the_password_appearing_verbatim(tmp_path):
    base = tmp_path / "run" / "profile"
    # Plant the secret in the file, then prove the read-back refuses it.
    H.write_profile(base, "f2src", 55432, "v3user")
    path = base / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8")
                    + "\nleaked: not-in-the-file-but-\n", encoding="utf-8")
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_no_credentials(path, secret="not-in-the-file-but-")
    assert "verbatim" in str(excinfo.value)


def test_profile_isolation_gate_requires_the_disposable_target(tmp_path):
    root = tmp_path / "run"
    path = H.write_profile(root / "profile", "f2e2e", 55432, "v3user")
    record = H.assert_profile_isolated(path, root, "f2e2e", 55432, "v3user")
    assert record["verdict"] == "PASS" and record["under_f2_root"] is True
    with pytest.raises(H.IsolationError):
        H.assert_profile_isolated(path, root, "f2e2e", 5433, "v3user")
    with pytest.raises(H.IsolationError):
        H.assert_profile_isolated(path, root, "v3embeddings", 55432, "v3user")
    with pytest.raises(H.IsolationError):
        H.assert_profile_isolated(root / "nope.yaml", root, "f2e2e", 55432, "v3user")
    # A config that exists but lives OUTSIDE the run root is the leak this
    # gate exists to catch, so it must be refused even though the file is
    # a perfectly valid profile.
    outsider = H.write_profile(tmp_path / "elsewhere", "f2e2e", 55432, "v3user")
    with pytest.raises(H.IsolationError):
        H.assert_profile_isolated(outsider, root, "f2e2e", 55432, "v3user")


# ══════════════════════════════════════════════════════════════════════════
# I6 — the child environment
# ══════════════════════════════════════════════════════════════════════════
def test_child_env_sets_test_mode_and_an_explicit_config(tmp_path):
    config = tmp_path / "run" / "profile" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("basePath: {}\n".format(tmp_path), encoding="utf-8")
    env = H.child_env(config, config.parent, "s3cr3t")
    assert env["V3CORE_TEST_MODE"] == "1"
    assert env["V3CORE_CONFIG"] == str(config)
    assert env["V3CORE_PG_PASSWORD"] == "s3cr3t"
    assert env["PGPASSWORD"] == "s3cr3t"
    # No ambient provider key survives, and no ambient dotenv can be found.
    for name in H.BLANKED_PROVIDER_ENV:
        assert env[name] == ""
    assert env["V3CORE_DOTENV"] == ""
    assert env["V3CORE_HOME"] == str(config.parent)
    assert env["HERMES_HOME"] == str(config.parent)


def test_child_env_applies_extra_overrides(tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("basePath: .\n", encoding="utf-8")
    env = H.child_env(config, tmp_path, "pw", extra={"F2_PROFILE": "default"})
    assert env["F2_PROFILE"] == "default"


def test_secret_redaction_covers_text_only():
    assert H.redact("pw", "a pw b") == "a <redacted> b"
    assert H.redact("", "untouched") == "untouched"
    assert H.redact("pw", 17) == 17
    assert H.scrub_dsn({"host": "h", "password": "pw"}) == {"host": "h"}


# ══════════════════════════════════════════════════════════════════════════
# PG allowlist
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("dsn,accept", [
    ("postgresql://u:p@127.0.0.1:55432/f2e2e", True),
    ("postgresql://u:p@localhost:55432/f2src", True),
    ("postgresql://u:p@127.0.0.1:55521/f2e2e", True),
    ("postgresql://u:p@10.0.0.5:55432/f2e2e", False),
    ("postgresql://u:p@127.0.0.1:5433/f2e2e", False),
    ("postgresql://u:p@127.0.0.1:55432/v3embeddings", False),
    ("postgresql://u:p@127.0.0.1:55432/postgres", False),
    ("postgresql://u:p@127.0.0.1:55432/my_dev", False),
    ("postgresql://u:p@127.0.0.1:55432/", False),
])
def test_pg_allowlist_admits_only_disposable_f2_databases(dsn, accept):
    target = H.parse_dsn(dsn)
    if accept:
        assert H.assert_disposable_target(target)["verdict"] == "PASS"
    else:
        with pytest.raises(H.PgTargetError):
            H.assert_disposable_target(target)


def test_pg_allowlist_excludes_the_production_database_by_name():
    assert "v3embeddings" not in H.ALLOWED_PG_DATABASES
    assert H.ALLOWED_PG_DATABASES == frozenset({"f2e2e", "f2src"})
    assert H.PROD_PG_PORT == 5433


def test_resolve_pg_target_requires_an_explicit_target(monkeypatch):
    monkeypatch.delenv("F2_PG_DSN", raising=False)
    with pytest.raises(H.PgTargetError) as excinfo:
        H.resolve_pg_target()
    assert "--pg-port" in str(excinfo.value)


def test_resolve_pg_target_builds_from_port_and_keeps_the_password_in_memory(monkeypatch):
    monkeypatch.delenv("F2_PG_DSN", raising=False)
    monkeypatch.setenv("F2_PG_PASSWORD", "from-env")
    target = H.resolve_pg_target(pg_port=55432, database="f2e2e", user="v3user")
    assert target["port"] == 55432 and target["database"] == "f2e2e"
    assert target["password"] == "from-env"
    # The DSN form takes the same password from the same channel.
    monkeypatch.setenv("F2_PG_DSN", "postgresql://v3user@127.0.0.1:55432/f2e2e")
    target = H.resolve_pg_target()
    assert target["user"] == "v3user" and target["password"] == "from-env"


def test_resolve_pg_target_refuses_a_production_dsn(monkeypatch):
    monkeypatch.setenv("F2_PG_DSN", "postgresql://v3user@127.0.0.1:5433/v3embeddings")
    with pytest.raises(H.PgTargetError):
        H.resolve_pg_target()


# ══════════════════════════════════════════════════════════════════════════
# I8 — process identity + owned cleanup
# ══════════════════════════════════════════════════════════════════════════
def test_identity_match_requires_all_three_components():
    want = _identity(100)
    assert H.identity_matches(want, dict(want))[0] is True
    assert H.identity_matches(want, None) == (False, "process is gone")
    assert H.identity_matches(want, dict(want, pid=101))[1] == "pid mismatch"
    recycled = dict(want, created="2026-10-02T09:09:09.0000000Z")
    ok, why = H.identity_matches(want, recycled)
    assert ok is False and "recycled" in why
    other = dict(want, cmdline="python -m v3core serve --port 39090")
    assert H.identity_matches(want, other)[1] == "command line mismatch"


def test_terminate_owned_refuses_to_signal_an_unverified_process():
    table = FakeTable(_identity(200))
    table.rows[200] = _identity(200, created="2026-10-02T11:11:11.0000000Z")  # recycled
    record = H.terminate_owned(_identity(200), killer=table.kill, lookup=table.lookup,
                               sleep=lambda _s: None)
    assert record["signalled"] is False
    assert "recycled" in record["reason"]
    assert 200 in table.rows, "an unverified process must never be killed"


def test_terminate_owned_kills_a_verified_process_and_reads_back():
    table = FakeTable(_identity(300))
    record = H.terminate_owned(_identity(300), killer=table.kill, lookup=table.lookup,
                               sleep=lambda _s: None)
    assert record["signalled"] is True and record["clean"] is True
    assert record["verdict"] == "PASS" and table.lookup(300) is None


def test_terminate_owned_reports_a_residual_process_as_not_clean():
    def immortal_kill(_pid: int) -> dict:
        return {"rc": 1, "out": "", "err": "access denied"}

    table = FakeTable(_identity(400))
    record = H.terminate_owned(_identity(400), killer=immortal_kill, lookup=table.lookup,
                               sleep=lambda _s: None, timeout=0.05)
    assert record["signalled"] is True
    assert record["clean"] is False and record["verdict"] == "FAIL"
    assert record["residual_after"] == [400]


def test_registry_aborts_when_an_owned_child_survives():
    table = FakeTable(_identity(500))
    registry = H.ChildRegistry(lookup=table.lookup, killer=table.kill)
    child = H.OwnedChild("child-serve", FakeProc(500), _identity(500))
    registry.add(child)
    assert registry.alive() == [{"role": "child-serve", "pid": 500}]
    with pytest.raises(H.LeftoverProcessError) as excinfo:
        registry.assert_no_leftovers()
    assert "survived cleanup" in str(excinfo.value)
    # Once it is really gone, the gate passes.
    table.rows.clear()
    assert registry.assert_no_leftovers()["clean"] is True


def test_registry_stop_all_covers_every_child():
    table = FakeTable(_identity(600), _identity(601))
    registry = H.ChildRegistry(lookup=table.lookup, killer=table.kill)
    for pid in (600, 601):
        registry.add(H.OwnedChild("child-serve", FakeProc(pid), _identity(pid)))
    records = registry.stop_all()
    assert {r["pid"] for r in records} == {600, 601}
    assert registry.alive() == []
    assert registry.assert_no_leftovers()["owned_children"] == 2


def test_owned_child_record_carries_pid_creation_and_cmdline():
    table = FakeTable(_identity(700))
    child = H.OwnedChild("child-serve", FakeProc(700), _identity(700),
                         meta={"tag": "t"})
    child._lookup = table.lookup
    record = child.record()
    assert record["pid"] == 700
    assert record["created"] == _identity(700)["created"]
    assert "child-serve" in record["cmdline"]
    assert record["tag"] == "t"
    assert child.verify()["alive"] is True
    table.rows.clear()
    assert child.verify()["alive"] is False


@WINDOWS_ONLY
def test_live_process_inventory_returns_identity_triples():
    """The real inventory, on this host, read-only.

    Not gated on Windows: the contract under test is the SHAPE of the
    identity triple, and this proves the triple is populated for a live pid
    (this process) rather than fabricated.
    """
    table = H.process_snapshot()
    assert table, "process inventory returned nothing"
    me = os.getpid()
    assert me in table, "the running process must appear in its own inventory"
    entry = table[me]
    assert entry["pid"] == me
    assert entry["created"], "a process with no creation time can never be verified"
    assert isinstance(entry["ppid"], int)
    assert H.process_identity(me) is not None
    # A pid that cannot exist has no identity, which is what "already dead"
    # looks like to the cleanup gate.
    assert H.process_identity(0) in (None, table.get(0))


# ══════════════════════════════════════════════════════════════════════════
# I9 — production sentinel
# ══════════════════════════════════════════════════════════════════════════
def test_fingerprint_tree_is_recursive_and_content_addressed(tmp_path):
    root = tmp_path / "profile"
    (root / "j" / "pending_live_buffer").mkdir(parents=True)
    (root / "config.yaml").write_text("basePath: x\n", encoding="utf-8")
    (root / "j" / "pending_live_buffer" / "a.json").write_text("{}", encoding="utf-8")
    fingerprint = H.fingerprint_tree(root)
    assert fingerprint["config.yaml"]["sha256"]
    assert "j/pending_live_buffer/a.json" in fingerprint
    missing = H.fingerprint_tree(tmp_path / "nope")
    assert missing["__root__"]["exists"] is False


@pytest.mark.parametrize("relative", [
    "config.yaml", "observer_state.json", "topic_matrix.npz", "topic_meta.json",
    "v3_topic.db", "v3_cards.db", "j/marker.json",
])
def test_sentinel_covers_the_files_a_run_could_touch(tmp_path, relative):
    """NPZ cache, SQLite, observer cursor, outbox — all in the fingerprint."""
    root = tmp_path / "profile"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 8)
    assert relative.replace("\\", "/") in H.fingerprint_tree(root)
    assert relative in H.PRODUCTION_SENTINEL_HIGHLIGHTS or \
        relative.split("/")[0] in H.PRODUCTION_SENTINEL_HIGHLIGHTS


def test_sentinel_diff_detects_a_touched_cursor(tmp_path):
    before = H.fingerprint_tree(tmp_path)
    (tmp_path / "observer_state.json").write_text("{}", encoding="utf-8")
    after = H.fingerprint_tree(tmp_path)
    changed = H.diff_fingerprints(before, after)
    assert any(c["path"] == "observer_state.json" for c in changed)
    with pytest.raises(H.IsolationError) as excinfo:
        H.assert_production_unchanged(before, after)
    assert "INVALID" in str(excinfo.value)
    assert H.assert_production_unchanged(after, after)["unchanged"] is True


def test_sentinel_reads_only_the_explicit_home(tmp_path):
    """The helper takes ``home=``; the tests never touch a real profile."""
    home = tmp_path / "fake-home"
    (home / ".v3-core" / "profiles" / "default").mkdir(parents=True)
    (home / ".v3-core" / "profiles" / "default" / "config.yaml").write_text(
        "basePath: y\n", encoding="utf-8")
    record = H.production_sentinel(home)
    assert record["profile_dir"].endswith(("default", "default/"))
    assert Path(record["profile_dir"]) == home / ".v3-core" / "profiles" / "default"
    assert record["exists"] is True
    assert record["file_count"] == 1
    assert H.production_profile_dir(home) == home / ".v3-core" / "profiles" / "default"


# ══════════════════════════════════════════════════════════════════════════
# barriers — W0 before_commit vs W3 after_commit
# ══════════════════════════════════════════════════════════════════════════
def test_barrier_phase_classification_separates_w0_from_w3():
    assert H.classify_barrier_phase(0) == "before_commit"
    assert H.classify_barrier_phase(1) == "after_commit"
    assert H.classify_barrier_phase(2) == "after_commit"


def test_flush_barrier_matches_only_its_identity():
    barrier = H.FlushBarrier("h1", "s1", "e1", Path("marker"))
    assert barrier.matches(("s1", "e1", "c", "user", "1", None, None, None, "h1"))
    assert not barrier.matches(("s1", "e2", "c", "user", "1", None, None, None, "h1"))
    assert not barrier.matches(("s2", "e1", "c", "user", "1", None, None, None, "h1"))
    assert not barrier.matches(("s1", "e1", "c", "user", "1", None, None, None, "h2"))
    # A legacy 8-tuple (no host slot) must not match a real-host barrier.
    assert not barrier.matches(("s1", "e1", "c", "user", "1", None, None, None))


def test_flush_barrier_announces_arrival_and_times_out_without_releasing(tmp_path):
    marker = tmp_path / "reached.json"
    release = tmp_path / "release"
    barrier = H.FlushBarrier("h", "s", "e", marker, release, timeout=0.2,
                             sleep=lambda _s: None)
    assert barrier.wait() is False
    assert barrier.reached is True and barrier.released is False
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["event"] == "flush_barrier_reached"
    assert payload["session_id"] == "s" and payload["event_id"] == "e"


def test_flush_barrier_releases_on_an_explicit_signal(tmp_path):
    marker, release = tmp_path / "m.json", tmp_path / "rel"
    barrier = H.FlushBarrier("h", "s", "e", marker, release, timeout=5.0,
                             sleep=lambda _s: time.sleep(0.01))

    def _release_soon() -> None:
        time.sleep(0.05)
        release.write_text("go", encoding="utf-8")

    threading.Thread(target=_release_soon, daemon=True).start()
    assert barrier.wait() is True and barrier.released is True


def test_flush_barrier_wrap_gates_only_the_matching_batch(tmp_path):
    """The seam gates the real flush; an unrelated item passes through."""
    calls: list[tuple] = []

    class FakeBuffer:
        # The harness wraps the bound ``_flush`` attribute, so the fake has
        # to expose a real one to capture.
        def _flush(self, batch_size=0, items=None):
            calls.append(tuple(items or ()))

    buffer = FakeBuffer()
    buffer._flush = FakeBuffer._flush.__get__(buffer, FakeBuffer)
    barrier = H.FlushBarrier("h", "s", "e", tmp_path / "m", timeout=0.1,
                             sleep=lambda _s: None)
    barrier.wrap(buffer)
    buffer._flush(batch_size=1, items=[("s", "e", "c", "user", "1", None, None, None, "h")])
    assert calls == [], "the gated item must not reach the insert"
    buffer._flush(batch_size=1, items=[("s", "other", "c", "user", "1", None, None, None, "h")])
    assert len(calls) == 1, "an unrelated item must flow through untouched"


def test_flush_barrier_does_not_turn_a_timeout_into_a_commit(tmp_path):
    """A timed-out barrier returns without inserting — never a silent write."""
    inserted: list[tuple] = []

    class FakeBuffer:
        def _real(self, batch_size=0, items=None):
            inserted.append(tuple(items or ()))

    buffer = FakeBuffer()
    buffer._flush = buffer._real
    H.FlushBarrier("h", "s", "e", tmp_path / "m", timeout=0.05,
                   sleep=lambda _s: None).wrap(buffer)
    buffer._flush(batch_size=1, items=[("s", "e", "c", "user", "1", None, None, None, "h")])
    assert inserted == []


# ══════════════════════════════════════════════════════════════════════════
# the fault-injecting proxies (loopback only, no v3core)
# ══════════════════════════════════════════════════════════════════════════
def test_blackholed_direction_delivers_no_bytes_to_the_caller():
    server = _EchoServer()
    proxy = H.DirectionProxy(server.port, blackhole="server->client").start()
    try:
        result = H.raw_post_through_proxy(proxy.port, {"hello": "world"},
                                          read_timeout=10)
    finally:
        proxy.stop()
    assert result["bytes_received"] == 0
    assert result["got_http_ack"] is False
    # The server DID answer — the bytes existed and were destroyed in here.
    assert proxy.record()["server_to_client_bytes"] > 0
    assert proxy.record()["client_to_server_bytes"] > 0


def test_swallowed_direction_raises_a_real_transport_error():
    """W1's fault: the request never arrives and the client sees a real error."""
    server = _EchoServer()
    proxy = H.DirectionProxy(server.port, blackhole="client->server").start()
    try:
        result = H.raw_post_through_proxy(proxy.port, {"hello": "world"},
                                          read_timeout=1.0)
    finally:
        proxy.stop()
    assert result["bytes_received"] == 0
    assert result["error"], "the caller must observe a real socket failure"
    assert proxy.record()["client_to_server_bytes"] == 0


def test_proxy_client_reset_is_a_test_only_fault():
    server = _EchoServer()
    proxy = H.DirectionProxy(server.port, blackhole="server->client").start()
    try:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        sock.sendall(b"POST /events HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n"
                     b"Connection: close\r\n\r\n{}")
        assert proxy.reset_client(timeout=10) is True
        sock.close()
    finally:
        proxy.stop()


# ══════════════════════════════════════════════════════════════════════════
# I4 — provider-call proof
# ══════════════════════════════════════════════════════════════════════════
def test_provider_scan_distinguishes_a_real_call_from_the_no_request_branch(tmp_path):
    clean = tmp_path / "clean.log"
    clean.write_text(
        "LiveBuffer embedding degraded: source_id=live/s/e class=NO_INPUT "
        "attempts=0\n"
        "LiveBuffer flush: 写入 1 条消息到 pg\n", encoding="utf-8")
    result = H.scan_provider_calls([clean])
    assert result["provider_call_count"] == 0
    assert result["no_request_confirmations"] == 1
    assert result["verdict"] == "PASS-ZERO-PROVIDER-CALLS"

    dirty = tmp_path / "dirty.log"
    dirty.write_text("INFO v3core.llm: LLM call: provider=minimax\n", encoding="utf-8")
    bad = H.scan_provider_calls([dirty])
    assert bad["provider_call_count"] == 1 and bad["verdict"] == "FAIL"


# ══════════════════════════════════════════════════════════════════════════
# ACCEPTANCE SEAL — the exit code is a function of the WHOLE evidence file
# ══════════════════════════════════════════════════════════════════════════
# Everything below is offline and pure: it builds evidence dicts by hand and
# asserts what evaluate_acceptance decides. No fault injection, no PG, no
# children — the acceptance RULE is testable on any machine, which is the
# point: a destructive acceptance verdict must not be verifiable only on the
# one box allowed to produce it.

def _guard_pass() -> dict:
    return {"verdict": "PASS"}


def _passing_evidence(cases=None, **overrides) -> dict:
    """A run that passed everything: full matrix, all guards, clean teardown."""
    steps: dict = {}
    for name in cases if cases is not None else H.FULL_MATRIX:
        steps[name] = {"verdict": "PASS", "problems": [], "identity_rows": 1,
                       "qa_rows": 1,
                       "section6": {
                           "case": name, "section6_ok": True,
                           "section6_problems": [],
                           "checks": {"canonical_rows": True,
                                      "no_duplicate_qa": True,
                                      "transport_converged": True,
                                      "isolated_data_root": True}}}
    for phase in H.MANDATORY_GUARD_PHASES:
        steps[phase] = _guard_pass()
    steps["process_cleanup"] = {"verdict": "PASS", "clean": True,
                                "alive_after": 0, "owned_children": 4,
                                "per_child": [{"pid": 1, "clean": True}]}
    steps["provider_calls"] = {"verdict": "PASS-ZERO-PROVIDER-CALLS",
                               "provider_call_count": 0}
    steps["production_sentinel_after"] = {"verdict": "PASS", "unchanged": True}
    steps["config_lifecycle"] = {"verdict": "PASS", "removed": ["c.yaml"],
                                 "config_yaml_remaining": [],
                                 "deleted_after_all_children_dead": True}
    steps["child_identity_agreement"] = {"verdict": "PASS", "agreed": True,
                                        "children": [{"role": "child-serve"}]}
    steps.update(overrides.pop("steps", {}))
    evidence = {"steps": steps, "summary": {k: v["verdict"] for k, v in
                                            evidence_summary_of(steps).items()}}
    evidence.update(overrides)
    return evidence


def evidence_summary_of(steps: dict) -> dict:
    return {name: steps[name] for name in H.FULL_MATRIX
            if name in steps and isinstance(steps.get(name), dict)}


def test_only_a_complete_clean_run_is_accepted():
    result = H.evaluate_acceptance(_passing_evidence())
    assert result["verdict"] == "PASS"
    assert result["label"] == "F2 ACCEPTED"
    assert result["exit_code"] == 0
    assert result["f2_done"] is True
    assert result["failures"] == []
    assert result["full_matrix"] is True


def test_no_evidence_at_all_is_never_a_pass():
    for empty in (None, {}, {"steps": {}}):
        result = H.evaluate_acceptance(empty)
        assert result["exit_code"] != 0
        assert result["f2_done"] is False
    assert H.evaluate_acceptance(None)["exit_code"] == H.EXIT_NO_EVIDENCE


# ── the core regression: all W PASS is NOT sufficient ────────────────────
@pytest.mark.parametrize("guard,broken", [
    ("isolated_profile", {"verdict": "FAIL", "error": "basePath outside root"}),
    ("database_recreate", {"verdict": "FAIL"}),
    ("fresh_bootstrap", {"verdict": "FAIL"}),
    ("schema_readback", {"verdict": "FAIL", "error": "no identity index"}),
    ("child_identity_agreement", {"verdict": "FAIL", "agreed": False}),
    ("production_sentinel_after", {"verdict": "FAIL", "unchanged": False}),
    ("production_sentinel_before", {"verdict": "FAIL"}),
    ("config_lifecycle", {"verdict": "FAIL", "removed": [],
                          "config_yaml_remaining": ["C:/run/profile/config.yaml"],
                          "deleted_after_all_children_dead": False}),
    ("process_cleanup", {"verdict": "FAIL", "clean": False, "alive_after": 0,
                         "per_child": []}),
])
def test_every_isolation_guard_failure_rejects_an_all_pass_matrix(guard, broken):
    """One failed guard is enough to refuse, even with W0-W4 all PASS."""
    evidence = _passing_evidence(steps={guard: broken})
    assert evidence["summary"] == {n: "PASS" for n in H.FULL_MATRIX}, \
        "precondition: the matrix itself passed"
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL"
    assert result["label"] == "F2 REJECTED"
    assert result["exit_code"] == H.EXIT_REJECTED != 0
    assert result["f2_done"] is False
    assert any(guard in line for line in result["failures"]), result["failures"]


@pytest.mark.parametrize("guard", list(H.MANDATORY_GUARD_PHASES))
def test_a_missing_mandatory_guard_phase_is_a_failure_not_a_pass(guard):
    evidence = _passing_evidence()
    del evidence["steps"][guard]
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL"
    assert result["exit_code"] != 0
    assert result["guards"][guard]["present"] is False
    assert any(guard in line for line in result["failures"])


def test_mandatory_guard_list_covers_every_isolation_invariant():
    """The seal is only as good as the list it enforces."""
    for phase in ("isolated_profile", "child_identity_agreement",
                  "process_cleanup", "production_sentinel_after",
                  "config_lifecycle", "schema_readback", "fresh_bootstrap",
                  "database_recreate"):
        assert phase in H.MANDATORY_GUARD_PHASES, phase
    assert len(set(H.MANDATORY_GUARD_PHASES)) == len(H.MANDATORY_GUARD_PHASES)
    # Mission §6: the provider-marker total is telemetry, so it must NOT be a
    # gate — gating it would fail correct runs whose retry adds one more
    # embed_for_write line.
    assert "provider_calls" not in H.MANDATORY_GUARD_PHASES
    assert "provider_calls" in H.RECORD_ONLY_GUARDS


@pytest.mark.parametrize("case", list(H.FULL_MATRIX))
def test_a_missing_w_result_rejects_the_run(case):
    evidence = _passing_evidence()
    del evidence["steps"][case]
    evidence["summary"].pop(case, None)
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL"
    assert result["exit_code"] != 0
    assert any(case in line for line in result["failures"])


@pytest.mark.parametrize("verdict", ["FAIL", "ERROR", None, "PARTIAL", "pass"])
def test_a_non_pass_case_verdict_rejects_the_run(verdict):
    evidence = _passing_evidence()
    evidence["steps"]["W3"]["verdict"] = verdict
    evidence["summary"]["W3"] = verdict
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL" and result["exit_code"] != 0


def test_run_error_rejects_an_otherwise_clean_run():
    evidence = _passing_evidence(steps={
        "run_error": {"error_type": "PgTargetError", "error": "no PG target"}})
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL"
    assert result["exit_code"] == H.EXIT_REJECTED
    assert any("run_error" in line for line in result["failures"])


def test_surviving_children_reject_the_run():
    evidence = _passing_evidence(steps={"process_cleanup": {
        "verdict": "PASS", "clean": False, "alive_after": 1,
        "leftovers": [{"role": "child-serve", "pid": 4242}],
        "per_child": [{"pid": 4242, "clean": False}]}})
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL" and result["exit_code"] != 0
    joined = " ".join(result["failures"])
    assert "4242" in joined or "still alive" in joined


def test_child_disagreement_rejects_the_run():
    evidence = _passing_evidence(steps={"child_identity_agreement": {
        "verdict": "PASS", "agreed": False, "children": []}})
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL" and result["exit_code"] != 0
    assert any("did not agree" in line for line in result["failures"])


def test_provider_markers_are_recorded_but_never_gate_the_run():
    """Mission §6: a non-zero provider-marker count is telemetry, not a FAIL.

    A repeated event legitimately adds one more ``embed_for_write`` line, and
    a synthetic run can trip a marker with no real external call. The gate on
    real provider calls is STRUCTURAL and lives in the guarded phases.
    """
    for count in (1, 3, 99):
        evidence = _passing_evidence(steps={"provider_calls": {
            "verdict": "FAIL", "provider_call_count": count,
            "files_with_markers": {"a.log": ["LiveBuffer embedding attempt"]}}})
        result = H.evaluate_acceptance(evidence)
        assert result["verdict"] == "PASS", \
            f"provider markers must not reject a clean run: {result['failures']}"
        assert result["exit_code"] == 0
        # ...but the count is still recorded, not discarded.
        assert result["guards"]["provider_calls"]["record_only"] is True
        assert result["guards"]["provider_calls"]["provider_call_count"] == count
    # A MISSING provider_calls step is likewise not a gate.
    evidence = _passing_evidence()
    del evidence["steps"]["provider_calls"]
    assert H.evaluate_acceptance(evidence)["verdict"] == "PASS"


def test_a_moved_production_sentinel_rejects_the_run():
    evidence = _passing_evidence(steps={"production_sentinel_after": {
        "verdict": "FAIL", "unchanged": False, "changed": ["config.yaml"]}})
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL" and result["exit_code"] != 0


def test_a_config_left_behind_rejects_the_run():
    evidence = _passing_evidence(steps={"config_lifecycle": {
        "verdict": "PASS", "removed": [], "config_yaml_remaining": ["c.yaml"],
        "deleted_after_all_children_dead": True}})
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL" and result["exit_code"] != 0
    assert any("config left on disk" in line for line in result["failures"])


# ── --only subset: PARTIAL, and never F2 DONE ────────────────────────────
@pytest.mark.parametrize("subset", [
    ("W0",), ("W0", "W1"), ("W1", "W2", "W3"), ("W4",), ("W0", "W2", "W4"),
])
def test_a_restricted_matrix_is_partial_and_never_f2_done(subset):
    evidence = _passing_evidence(cases=subset)
    result = H.evaluate_acceptance(evidence, selected=list(subset))
    assert result["verdict"] == "PARTIAL"
    assert result["label"] != "F2 ACCEPTED"
    assert "NOT F2 DONE" in result["label"]
    assert result["f2_done"] is False
    assert result["exit_code"] == H.EXIT_PARTIAL != 0
    assert result["failures"] == [], "a subset run is not broken, just partial"
    assert any("full matrix" in line for line in result["withheld"])


def test_a_subset_that_also_broke_a_guard_is_rejected_not_partial():
    """PARTIAL never downgrades a real failure."""
    evidence = _passing_evidence(cases=("W0", "W1"),
                                 steps={"production_sentinel_after": {
                                     "verdict": "FAIL", "unchanged": False}})
    result = H.evaluate_acceptance(evidence, selected=["W0", "W1"])
    assert result["verdict"] == "FAIL"
    assert result["exit_code"] == H.EXIT_REJECTED


def test_evaluate_acceptance_is_pure():
    """Same evidence in, same verdict out — and the input is not mutated."""
    evidence = _passing_evidence()
    snapshot = json.dumps(evidence, sort_keys=True, default=str)
    first = H.evaluate_acceptance(evidence)
    second = H.evaluate_acceptance(evidence)
    assert first == second
    assert json.dumps(evidence, sort_keys=True, default=str) == snapshot


# ══════════════════════════════════════════════════════════════════════════
# teardown is fail-closed: a cleanup exception is a rejection, and the config
# is PRESERVED rather than deleted
# ══════════════════════════════════════════════════════════════════════════
class _ExplodingChild(H.OwnedChild):
    """A child whose termination blows up — a cleanup fault, not a stop.

    ``stopped`` is deliberately NOT set: the harness must not be able to
    conclude the process is gone when the stop itself failed. The leftover
    then has to show up in ``ChildRegistry.alive`` and in the evidence.
    """

    def stop(self, timeout: float = 30.0, force: bool = True) -> dict:
        raise RuntimeError(f"taskkill blew up for pid {self.identity['pid']}")


def test_stop_all_attempts_every_child_then_raises_instead_of_hiding_it():
    table = FakeTable(_identity(800), _identity(801))
    registry = H.ChildRegistry(lookup=table.lookup, killer=table.kill)
    registry.add(_ExplodingChild("child-serve", FakeProc(800), _identity(800)))
    registry.add(H.OwnedChild("child-client", FakeProc(801), _identity(801)))
    with pytest.raises(H.ChildCleanupError) as excinfo:
        registry.stop_all()
    assert "800" in str(excinfo.value)
    # The sibling was still stopped: one failure must not strand the others.
    assert 801 not in table.rows
    assert table.lookup(800) is not None, "the exploded child is still alive"


def test_dispose_records_a_failed_process_cleanup_and_reraises(tmp_path):
    table = FakeTable(_identity(810))
    registry = H.ChildRegistry(lookup=table.lookup, killer=table.kill)
    registry.add(_ExplodingChild("child-serve", FakeProc(810), _identity(810)))
    ctx = _stub_ctx(tmp_path, registry)
    with pytest.raises(H.ChildCleanupError):
        ctx.dispose()
    record = ctx.evidence["steps"]["process_cleanup"]
    assert record["verdict"] == "FAIL"
    assert record["clean"] is False
    assert record["alive_after"] == 1


def test_teardown_preserves_the_config_when_cleanup_throws(tmp_path):
    table = FakeTable(_identity(820))
    registry = H.ChildRegistry(lookup=table.lookup, killer=table.kill)
    registry.add(_ExplodingChild("child-serve", FakeProc(820), _identity(820)))
    ctx = _stub_ctx(tmp_path, registry)
    report = H.finalize_teardown(ctx, None)
    assert report["config_preserved"] is True
    assert report["errors"], "a cleanup fault must be recorded, not swallowed"
    lifecycle = ctx.evidence["steps"]["config_lifecycle"]
    assert lifecycle["verdict"] == "FAIL"
    assert lifecycle["deleted"] is False
    assert ctx.config_path.is_file(), "the config must survive a failed teardown"
    # ...and the run it feeds is rejected, not passed.
    acceptance = H.evaluate_acceptance(ctx.evidence, selected=list(H.FULL_MATRIX))
    assert acceptance["exit_code"] != 0
    assert acceptance["f2_done"] is False


def test_a_clean_teardown_does_delete_the_config(tmp_path):
    table = FakeTable(_identity(830))
    registry = H.ChildRegistry(lookup=table.lookup, killer=table.kill)
    registry.add(H.OwnedChild("child-serve", FakeProc(830), _identity(830)))
    ctx = _stub_ctx(tmp_path, registry)
    report = H.finalize_teardown(ctx, None)
    assert report["config_preserved"] is False
    assert report["errors"] == []
    lifecycle = ctx.evidence["steps"]["config_lifecycle"]
    assert lifecycle["verdict"] == "PASS"
    assert lifecycle["deleted_after_all_children_dead"] is True
    assert not ctx.config_path.exists()


def test_a_teardown_error_in_evidence_rejects_the_run():
    evidence = _passing_evidence(steps={"teardown": {
        "errors": ["process_cleanup: RuntimeError: taskkill blew up"],
        "config_preserved": True}})
    result = H.evaluate_acceptance(evidence)
    assert result["verdict"] == "FAIL" and result["exit_code"] != 0
    assert any("teardown" in line for line in result["failures"])


def _stub_ctx(tmp_path, registry) -> H.RunContext:
    """A RunContext with a real config file and a fake process table."""
    target = {"host": "127.0.0.1", "port": 55432, "database": "f2e2e",
              "user": "v3user", "password": ""}
    ctx = H.RunContext(tmp_path / "run", target, None, tmp_path)
    ctx.registry = registry
    ctx.prepare()
    return ctx


# ══════════════════════════════════════════════════════════════════════════
# OwnedChild.wait_event must read the flag the class actually initializes.
#
# ``__init__`` sets ``self.stopped = False`` (line 783) and ``stop()`` sets it on
# the successful path, but ``wait_event`` tested ``self._stopped`` — an
# attribute that exists nowhere. So the ONE branch whose job is to turn a child
# that died without announcing itself into a classifiable, actionable
# F2HarnessError instead raised AttributeError: an exited child surfaced with the
# wrong failure type, losing the rc, the role and the tail lines the message
# carries. And because ``stop()`` writes ``stopped``, the branch was supposed to
# stay silent for a child we terminated ourselves — that suppression was dead
# code too.
#
# Bounded and process-free: a FakeProc that has already exited. No spawn, no
# signal, no real child; two short timeouts.
# ══════════════════════════════════════════════════════════════════════════
def test_wait_event_reports_an_exited_child_as_a_harness_error_not_an_attribute_error():
    """REGRESSION: no AttributeError from the nonexistent ``_stopped`` flag."""
    child = H.OwnedChild("child-serve", FakeProc(9990, returncode=3),
                         _identity(9990))
    # The class initializes exactly one stop flag; assert the code reads that one.
    assert child.__dict__["stopped"] is False
    assert "_stopped" not in child.__dict__

    with pytest.raises(H.F2HarnessError) as excinfo:
        child.wait_event("flush_barrier_reached", timeout=0.2)
    message = str(excinfo.value)
    assert "child-serve" in message
    assert "exited" in message and "rc=3" in message
    assert "flush_barrier_reached" in message


def test_wait_event_on_an_exited_child_is_not_a_stopped_child():
    """A stopped child must not be mistaken for one that exited un-announced.

    ``stop()`` sets ``stopped``; a child we terminated ourselves exiting without
    its event is our own doing, not a crash to report. The exit branch must
    therefore key off the SAME flag ``stop()`` sets — which is what the
    ``_stopped`` typo broke.
    """
    table = FakeTable(_identity(9991))
    child = H.OwnedChild("child-serve", FakeProc(9991, returncode=0),
                         _identity(9991), lookup=table.lookup,
                         killer=table.kill)
    table.kill(9991)                      # the process is gone
    child.stopped = True                  # as stop() would have set it

    # Stopped: wait_event keeps waiting (and finally times out on its own bound)
    # instead of claiming the child crashed.
    with pytest.raises(H.F2HarnessError) as excinfo:
        child.wait_event("never_emitted", timeout=0.1)
    assert "never emitted" in str(excinfo.value)
    assert "exited" not in str(excinfo.value)


def test_owned_child_stop_sets_the_flag_wait_event_reads():
    """The two must agree on the name, or the exit branch is dead code."""
    table = FakeTable(_identity(9992))
    child = H.OwnedChild("child-client", FakeProc(9992, returncode=0),
                         _identity(9992), lookup=table.lookup, killer=table.kill)
    assert child.stopped is False
    record = child.stop(timeout=0.05)
    assert record["clean"] is True
    assert child.stopped is True
    assert child.stop_record is record
    # Idempotent: a second stop returns the same record, no second signal.
    assert child.stop(timeout=0.05) is record


# ══════════════════════════════════════════════════════════════════════════
# run_make itself: the exit code comes from the seal, not from the W summary
# ══════════════════════════════════════════════════════════════════════════
def _make_args(**overrides) -> argparse.Namespace:
    base = dict(root="C:/hp-testbed/f2-test-run", dsn="", pg_port=55432,
                pg_database="f2e2e", pg_user="", output="", only="",
                repo_root="")
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_make_exit_code_is_the_seal_not_the_w_summary(monkeypatch, tmp_path):
    """All five cases PASS, one GUARD fails -> nonzero, verdict in evidence.

    The precondition is the whole point: under the old rule this W summary
    alone exited 0.
    """
    monkeypatch.setattr(H, "assert_test_host",
                        lambda *a, **k: {"hostname": "DESKTOP-EQP3OBU",
                                         "verdict": "PASS"})
    monkeypatch.setattr(H, "production_sentinel",
                        lambda *a, **k: {"profile_dir": "x", "fingerprint": {},
                                         "file_count": 0})
    monkeypatch.setattr(H, "recreate_database", lambda *a, **k: {"verdict": "PASS"})
    monkeypatch.setattr(H, "bootstrap_schema", lambda *a, **k: {"verdict": "PASS"})
    monkeypatch.setattr(H, "schema_readback", lambda *a, **k: {"verdict": "PASS"})
    monkeypatch.setattr(H, "assert_all_children_agree",
                        lambda *a, **k: (_ for _ in ()).throw(
                            H.IsolationError("children resolved DIFFERENT data roots")))
    for name in H.FULL_MATRIX:
        monkeypatch.setitem(H.W_CASES, name,
                            lambda ctx: {"verdict": "PASS", "identity_rows": 1})
    out = tmp_path / "evidence.json"
    rc = H.run_make(_make_args(root=str(tmp_path / "run"), output=str(out)))
    assert rc != 0
    evidence = json.loads(out.read_text(encoding="utf-8"))
    acceptance = evidence["acceptance"]
    # The precondition that makes this test bite: the W summary on its own
    # would have exited 0 under the old rule.
    assert evidence["summary"] == {n: "PASS" for n in H.FULL_MATRIX}, \
        f"the W summary alone must be all-PASS here: {evidence['summary']}"
    assert acceptance["verdict"] == "FAIL"
    assert acceptance["f2_done"] is False
    assert any("child_identity_agreement" in line
               for line in acceptance["failures"])


def test_run_make_gates_on_partial_matrix(monkeypatch, tmp_path):
    """``--only W0`` with everything green is PARTIAL with its own rc."""
    monkeypatch.setattr(H, "assert_test_host",
                        lambda *a, **k: {"hostname": "DESKTOP-EQP3OBU",
                                         "verdict": "PASS"})
    monkeypatch.setattr(H, "production_sentinel",
                        lambda *a, **k: {"profile_dir": "x", "fingerprint": {},
                                         "file_count": 0})
    monkeypatch.setattr(H, "recreate_database", lambda *a, **k: {"verdict": "PASS"})
    monkeypatch.setattr(H, "bootstrap_schema", lambda *a, **k: {"verdict": "PASS"})
    monkeypatch.setattr(H, "schema_readback", lambda *a, **k: {"verdict": "PASS"})
    monkeypatch.setattr(H, "assert_all_children_agree",
                        lambda *a, **k: {"agreed": True, "verdict": "PASS"})
    monkeypatch.setattr(H, "scan_provider_calls",
                        lambda *a, **k: {"provider_call_count": 0,
                                         "verdict": "PASS-ZERO-PROVIDER-CALLS"})
    monkeypatch.setitem(H.W_CASES, "W0", lambda ctx: {
        "verdict": "PASS", "identity_rows": 1, "qa_rows": 1,
        # §6 is evaluated by every real case; the stub has to carry it too,
        # or this test would be exercising the missing-proof path instead of
        # the partial-matrix path it is named for.
        "section6": {"case": "W0", "section6_ok": True,
                     "section6_problems": [],
                     "checks": {"canonical_rows": True,
                                "no_duplicate_qa": True,
                                "transport_converged": True,
                                "isolated_data_root": True}}})
    out = tmp_path / "evidence.json"
    rc = H.run_make(_make_args(root=str(tmp_path / "run"), only="W0",
                               output=str(out)))
    assert rc == H.EXIT_PARTIAL != 0
    evidence = json.loads(out.read_text(encoding="utf-8"))
    acceptance = evidence["acceptance"]
    assert acceptance["verdict"] == "PARTIAL"
    assert acceptance["f2_done"] is False
    assert acceptance["full_matrix"] is False
    assert "NOT F2 DONE" in acceptance["label"]


def test_run_make_records_a_run_error_and_rejects(monkeypatch, tmp_path):
    """A run that died mid-flight is never a pass, whatever the cases said."""
    monkeypatch.setattr(H, "assert_test_host",
                        lambda *a, **k: {"hostname": "DESKTOP-EQP3OBU",
                                         "verdict": "PASS"})
    monkeypatch.setattr(H, "production_sentinel",
                        lambda *a, **k: {"profile_dir": "x", "fingerprint": {},
                                         "file_count": 0})
    monkeypatch.setattr(H, "recreate_database", lambda *a, **k: {"verdict": "PASS"})
    # The bootstrap dies AFTER the run has a context, so a run_error step is
    # recorded and no case ever ran.
    monkeypatch.setattr(H, "bootstrap_schema",
                        lambda *a, **k: (_ for _ in ()).throw(
                            H.F2HarnessError("alpha_bootstrap.sql is missing")))
    out = tmp_path / "evidence.json"
    rc = H.run_make(_make_args(root=str(tmp_path / "run"), output=str(out)))
    assert rc != 0
    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert "run_error" in evidence["steps"]
    assert evidence["acceptance"]["verdict"] == "FAIL"
    assert evidence["acceptance"]["f2_done"] is False
    assert any("run_error" in line
               for line in evidence["acceptance"]["failures"])


def test_run_make_without_evidence_never_exits_zero(monkeypatch, tmp_path):
    """A refusal before any context exists still reports nonzero."""
    monkeypatch.setattr(H, "assert_test_host",
                        lambda *a, **k: (_ for _ in ()).throw(
                            H.HostGateError("DESKTOP-MU2RQIU is PRODUCTION")))
    out = tmp_path / "evidence.json"
    rc = H.run_make(_make_args(root=str(tmp_path / "run"), output=str(out)))
    assert rc != 0
    assert not out.exists()


# ══════════════════════════════════════════════════════════════════════════
# run_w0 executes end to end against a mock ctx — no fault injection
# ══════════════════════════════════════════════════════════════════════════
class _MockCtx:
    """Just enough RunContext surface for :func:`H.run_w0` to run.

    Everything W0 touches is faked: no PG, no child process, no proxy socket,
    no crash injection. The point is that the control flow AFTER the barrier
    — the retry, the read-backs, the child stops, the final problem list —
    actually executes and returns a verdict.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = Path(tmp_path)
        self.base = self.root / "profile"
        self.logs = self.root / "logs"
        self.barriers = self.root / "barriers"
        for path in (self.logs, self.barriers):
            path.mkdir(parents=True, exist_ok=True)
        self.target = {"host": "127.0.0.1", "port": 55432, "database": "f2e2e",
                       "user": "v3user", "password": ""}
        self.repo_root = self.root
        self.registry = None
        self.children: list[dict] = []
        self.steps: dict = {}
        self.serve_tags: list[str] = []

    def step(self, name, record):
        self.steps[name] = record
        return record

    def env(self, extra=None):
        return {}

    def serve_child(self, tag, barrier=None):
        self.serve_tags.append(tag)
        child = _MockChild(f"serve_{tag}", port=20000 + len(self.serve_tags))
        self.children.append({"tag": tag, "port": child.port,
                              "role": "child-serve"})
        return child


class _MockChild:
    def __init__(self, name: str, port: int = 0) -> None:
        self.name = name
        self.port = port
        self.identity = {"pid": 4242}
        self.stopped = False
        self.stop_count = 0

    def stop(self, timeout=30.0, force=True):
        self.stop_count += 1
        self.stopped = True
        return {"pid": self.identity["pid"], "signalled": True, "clean": True,
                "residual_after": [], "verdict": "PASS"}

    def wait_event(self, name, timeout=180.0):
        if name == "client_result":
            return {"bytes_received": 0, "got_http_ack": False,
                    "error": "ConnectionResetError", "outcome": "no-ack"}
        return {"port": 1, "pid": self.identity["pid"], "event": name}


class _MockProxy:
    def __init__(self, *a, **k) -> None:
        self.port = 31000

    def start(self):
        return self

    def reset_client(self, timeout=30.0):
        return True

    def stop(self):
        return None

    def record(self):
        return {"port": self.port, "blackholed": "server->client",
                "client_to_server_bytes": 100, "server_to_client_bytes": 220}


def _mock_w0_env(monkeypatch, barrier_rows: int, final_rows: int = 1) -> None:
    """Fake every W0 collaborator. ``barrier_rows`` must be 0 for a valid W0."""
    seen = {"n": 0}

    def _readback(target, host, session, event):
        seen["n"] += 1
        count = barrier_rows if seen["n"] == 1 else final_rows
        return {"identity": {"host": host, "session_id": session,
                             "event_id": event}, "row_count": count, "rows": []}

    monkeypatch.setattr(H, "DirectionProxy", _MockProxy)
    monkeypatch.setattr(H, "identity_readback", _readback)
    monkeypatch.setattr(H, "wait_identity_count",
                        lambda target, host, session, event, want,
                        timeout=420.0: final_rows)
    monkeypatch.setattr(H, "post_event",
                        lambda port, payload, timeout=120.0: (
                            200, {"ok": True, "accepted": True, "duplicate": False}))
    monkeypatch.setattr(H, "spawn_child",
                        lambda role, args, env, workdir, stderr, registry=None:
                        _MockChild("client_w0"))
    # The mission §6 collaborators W0 now also evaluates. A converged end
    # state is the only one that lets W0 report PASS.
    monkeypatch.setattr(H, "marker_state",
                        lambda root, host, session, event: {
                            "identity": {"host": host, "session_id": session,
                                         "event_id": event},
                            "identity_hash": "x", "host_namespace": "namespaced",
                            "expected_accepted_path": "p", "accepted_present": True,
                            "accepted_files": [], "pending_present": False,
                            "pending_files": [], "converged": True})
    monkeypatch.setattr(H, "qa_rows_for_session", lambda target, session: 1)


@pytest.mark.parametrize("duplicate", [False, True])
def test_w0_runs_to_completion_on_a_mock_ctx(monkeypatch, tmp_path, duplicate):
    """W0 returns a real verdict instead of dying before the retry.

    Guards a real regression: run_w0 once carried a bare ``ctx.dispose``
    expression that did nothing, so the tail of every otherwise-successful
    W0 was a no-op. Driving the whole function here proves the tail executes
    and the case reports rather than erroring.
    """
    ctx = _MockCtx(tmp_path)
    (ctx.barriers / "w0.reached").write_text("{}", encoding="utf-8")
    _mock_w0_env(monkeypatch, barrier_rows=0, final_rows=1)
    monkeypatch.setattr(H, "post_event", lambda *args, **kwargs: (
        200, {"ok": True, "accepted": not duplicate, "duplicate": duplicate}))

    result = H.run_w0(ctx)
    assert result["verdict"] == "PASS", result["problems"]
    assert result["problems"] == []
    assert result["barrier_phase"] == "before_commit"
    assert result["rows_at_barrier"] == 0
    assert result["caller_bytes_received"] == 0
    assert result["identity_rows"] == 1
    assert result["retry"]["http"] == 200
    # Both serve children ran: w0_a is the crash victim, w0_b the restart that
    # performs the retry. Reaching the retry is the tail this test covers.
    assert ctx.serve_tags == ["w0_a", "w0_b"]
    assert "w0_barrier" in ctx.steps


def test_w0_still_rejects_a_wrong_barrier_phase(monkeypatch, tmp_path):
    """The mocked path judges rather than merely executes."""
    ctx = _MockCtx(tmp_path)
    (ctx.barriers / "w0.reached").write_text("{}", encoding="utf-8")
    _mock_w0_env(monkeypatch, barrier_rows=1, final_rows=1)

    result = H.run_w0(ctx)
    assert result["verdict"] == "FAIL"
    assert any("before_commit" in p for p in result["problems"])


def test_w0_reports_a_duplicate_on_the_restart_retry(monkeypatch, tmp_path):
    """A non-zero final row count is a real problem, not a silent pass."""
    ctx = _MockCtx(tmp_path)
    (ctx.barriers / "w0.reached").write_text("{}", encoding="utf-8")
    _mock_w0_env(monkeypatch, barrier_rows=0, final_rows=2)

    result = H.run_w0(ctx)
    assert result["verdict"] == "FAIL"
    assert any("2" in p for p in result["problems"])


def test_barrier_is_installed_on_the_core_serve_actually_builds(monkeypatch, tmp_path):
    """B1: the barrier must gate the buffer the HTTP handler writes through.

    ``v3core.serve.serve()`` constructs its OWN ``V3Core`` internally
    (serve.py:399-401) and hands it to ``build_handler``. A harness that wraps
    a core it created itself therefore instruments a buffer that will never
    receive an HTTP event, and W0 blocks out its full 600 s wait.

    The regression drives the real ``V3Core`` class through the real
    ``live_buffer`` property and the real handler factory, with the network
    call faked: it proves the instrumented buffer is the SAME object the
    handler captures, and that a flush through it reaches the barrier.
    """
    from v3core import V3Core
    from v3core.ingest import LiveBuffer
    from v3core.serve import build_handler

    # The real V3Core resolves its own config, so the test boots it against an
    # ISOLATED profile written by the harness itself — the same writer and the
    # same child env the real child uses. No PG server is needed or contacted:
    # with no connected pool the live_buffer is built with pg=None, which is
    # still the real LiveBuffer and the real handler path.
    base = tmp_path / "profile"
    H.write_profile(base, "f2e2e", 55432, "v3user")
    for key, value in H.child_env(base / "config.yaml", base, "harness-test-pw").items():
        monkeypatch.setenv(key, value)

    marker = tmp_path / "barrier.json"
    release = tmp_path / "release"
    barrier = {"host": "h", "session_id": "s", "event_id": "e",
               "marker_path": str(marker), "release_path": str(release),
               "timeout": 5.0}

    # The handler is built from the core serve() makes, and the writer call
    # goes through that core's live_buffer — exactly the W0 path.
    served: dict = {}

    def _fake_serve(host, port, profile, ready_json=False):
        core = V3Core(profile=profile)
        core.initialize()
        served["core"] = core
        served["buffer"] = core.live_buffer
        served["handler"] = build_handler(core, {})
        return None

    arm = H.arm_barrier_on_served_core(barrier)
    try:
        _fake_serve("127.0.0.1", 0, "default")
        other = V3Core(profile="default")
        other.initialize()
        other_buffer = other.live_buffer

        assert served["core"] is not None, "serve() never built its core"
        # The handler captured a core, and the buffer we instrumented IS that
        # core's live buffer — this is the whole point of the fix.
        assert served["handler"].core is served["core"]
        assert served["buffer"] is served["core"].live_buffer
        assert arm.installed_on() is True
        assert served["buffer"] in arm.buffers
        # The instrumented object is the REAL LiveBuffer class, not a stand-in.
        assert isinstance(served["buffer"], LiveBuffer)
        assert served["buffer"].__dict__.get("_flush") is not None
        # A different, separately-created core gets its OWN wrapped buffer:
        # the seam follows whichever core the handler holds, it is not pinned
        # to one incidental instance.
        assert other_buffer in arm.buffers
        assert other_buffer is not served["buffer"]
    finally:
        if "other" in locals():
            other.shutdown()
        if served.get("core") is not None:
            served["core"].shutdown()
        arm.restore()

    # After the restore the product class is exactly as it was.
    assert V3Core.live_buffer is not None
    assert "__wrapped__" not in getattr(V3Core.live_buffer, "fget",
                                        type("x", (), {"__wrapped__": None})).__dict__


# ══════════════════════════════════════════════════════════════════════════
# W-case registry: each case is real, reports counts, and does not relabel
# ══════════════════════════════════════════════════════════════════════════
def test_every_w_case_is_registered_and_reports_counts_and_readback():
    assert set(H.W_CASES) == {"W0", "W1", "W2", "W3", "W4"}
    assert H.FULL_MATRIX == ("W0", "W1", "W2", "W3", "W4")
    for name, fn in H.W_CASES.items():
        assert callable(fn), name


def test_declared_matrix_matches_the_mission_section6_definitions():
    """§6 owns the labels. The docstring must not drift back to a relabelled
    matrix: each §6 phrase has to be present verbatim."""
    text = Path(H.__file__).read_text(encoding="utf-8")
    for claim in ("no ACK", "BEFORE the PG commit", "exactly 1 row",
                  "BEFORE the accepted tombstone", "SURVIVES",
                  "REPEATED crash/recover", "at least 3 cycles",
                  "canonical source rows = 1",
                  "no duplicate QA caused by same event",
                  "pending/accepted transport converges",
                  "no foreign data root",
                  "no production config/profile",
                  "no production DB",
                  "after_commit", "before_commit", "pre_tombstone"):
        assert claim in text, f"matrix documentation lost {claim!r}"

    # The definitions the earlier matrix wrongly used must be GONE, so a
    # later edit cannot quietly reintroduce the transport-fault / multi-host /
    # concurrency cases under these names.
    for retired in ("repeated request failures", "different hosts -> 2",
                    "concurrent same-identity clients"):
        assert retired not in text, f"retired W-case definition {retired!r} is back"


def test_w1_is_the_post_commit_pre_tombstone_window():
    """W1 must park the accepted tombstone AFTER the commit, not the flush."""
    import inspect
    src = inspect.getsource(H.run_w1)
    assert "tombstone=" in src, "W1 must arm the tombstone barrier"
    assert "pre_tombstone" in src
    assert 'at_barrier["row_count"] != 1' in src, (
        "W1 is the window AFTER the commit, so it requires one row at the "
        "barrier — this is what distinguishes it from W0")
    assert "classify_marker_phase" in src
    # It must restart into a FRESH process: a same-process retry is
    # short-circuited above PG and proves nothing.
    assert 'ctx.serve_child("w1_b")' in src
    assert 'ctx.serve_child("w1_a", tombstone=tombstone)' in src


def test_w2_requires_the_torn_transport_state_to_be_observed():
    """§6 W2 is 'accepted persisted but pending survives' — the case must
    FAIL when that state never occurred, not pass on row count alone."""
    import inspect
    src = inspect.getsource(H.run_w2)
    assert "pending_survived" in src
    assert 'if not at_crash["pending_present"]' in src, (
        "W2 must fail when no pending marker survived alongside the tombstone")
    assert 'if not at_crash["accepted_present"]' in src
    assert 'ctx.serve_child("w2_b")' in src
    # ...and it must CONVERGE: the surviving pending item has to be gone after
    # recovery even when the row count is right.
    assert 'if final_markers["pending_present"]' in src, (
        "W2's whole point is that recovery removes the surviving pending item")
    assert 'if not final_markers["accepted_present"]' in src


def test_w2_parks_the_pending_unlink_between_the_products_own_two_writes(tmp_path):
    """W2's torn state is HELD between the product's writes, not raced for.

    ``_ack_live_item`` persists the accepted tombstone and then unlinks the
    pending marker as the very next statement (``ingest.py:455-467``), so the
    old shape — release the tombstone barrier, then kill the tree — only
    SOMETIMES left the pending marker alive. The seam now calls the REAL
    ``_persist_live_accepted`` and parks the item right there, so the parked
    state is the product's own: PG=1, accepted durable, pending still on
    disk. Neither marker is authored by the harness, and the unlink stays
    the product's — it just happens after the kill, in the next process.
    """
    import inspect
    src = inspect.getsource(H.run_w2)
    assert 'ctx.serve_child("w2_a", post_accepted=post_accepted)' in src, (
        "W2 must arm the post-accepted seam, not the pre-tombstone one")
    assert "tombstone=tombstone" not in src
    assert ".release" not in src, (
        "W2 must not release the barrier and hope the unlink loses the race")
    assert "arm_post_accepted_barrier_on_served_core" in inspect.getsource(
        H._run_serve_child)
    # ...and the crash-side state it judges on is read AFTER the kill, so the
    # evidence is what the fresh process actually starts from.
    assert "at_crash = marker_state(" in src

    accepted = tmp_path / "accepted_live_buffer" / "h" / "ident.json"
    pending_dir = tmp_path / "pending_live_buffer"
    pending = pending_dir / "e.json"
    other_pending = pending_dir / "other.json"
    accepted.parent.mkdir(parents=True)
    pending_dir.mkdir(parents=True)
    for path in (pending, other_pending):
        path.write_text("{}", encoding="utf-8")

    class FakeBuffer:
        """The product's own two writes, in the product's own order."""

        def _persist_live_accepted(self, item, host=""):
            accepted.write_text('{"accepted": true}', encoding="utf-8")
            return True

        def _ack_live_item(self, item):
            (pending_dir / f"{item[1]}.json").unlink(missing_ok=True)

    buffer = FakeBuffer()
    marker = tmp_path / "w2.post_accepted_reached"
    barrier = H.PostAcceptedBarrier("h", "s", "e", marker, timeout=30.0,
                                    sleep=lambda _s: time.sleep(0.01))
    barrier.wrap(buffer)
    item = ("s", "e", "c", "user", "1", None, None, None, "h")
    other = ("s", "other", "c", "user", "1", None, None, None, "h")

    done = threading.Event()
    threading.Thread(target=lambda: (buffer._ack_live_item(item), done.set()),
                     daemon=True).start()
    deadline = time.time() + 30
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "the barrier never announced its arrival"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["event"] == "post_accepted_barrier_reached"
    assert payload["session_id"] == "s" and payload["event_id"] == "e"

    # PARKED: the tombstone is already durable, the pending marker has NOT been
    # unlinked, and the call has not returned. That is the §6 W2 state.
    assert barrier.accepted_persisted is True
    assert accepted.is_file()
    assert pending.is_file(), "the pending marker must survive while parked"
    assert not done.is_set(), "the item must still be held at the barrier"

    # An unrelated identity is never gated: it acks straight through.
    buffer._ack_live_item(other)
    assert not other_pending.exists(), "an unrelated item must not be gated"

    # Release (or a kill, which is what W2 actually does) lets the product
    # perform the unlink. Nobody else ever does.
    (tmp_path / (marker.name + ".release")).write_text("go", encoding="utf-8")
    assert done.wait(30)
    assert barrier.released is True
    assert accepted.is_file()
    assert not pending.exists(), "release must let the product unlink the marker"


@pytest.mark.parametrize("status,ack,rejected", [
    # Every one of these is a recovery that never told the caller the event
    # was durable, and every one is rejected even with row_count == 1.
    (500, {"ok": True, "accepted": True, "duplicate": False}, True),
    (200, {"ok": False, "accepted": True, "duplicate": False}, True),
    (200, {"ok": True, "accepted": False, "duplicate": False}, True),
    (200, {"ok": True, "duplicate": False}, True),
    (404, {}, True),
    # ...and these two are genuine outcomes, so they must NOT be rejected.
    (200, {"ok": True, "accepted": True, "duplicate": False}, False),
    (200, {"ok": True, "accepted": False, "duplicate": True}, False),
])
def test_w4_judges_each_recovery_on_its_ack_not_only_its_row_count(
        monkeypatch, tmp_path, status, ack, rejected):
    """§6 W4 cycles must each end in a real ACK, not just a right row count.

    A fresh process that answers non-200, answers ``ok=False``, or answers
    neither ``accepted`` nor ``duplicate`` never confirmed the event was
    durable, so the cycle is rejected. The row count is held at exactly 1
    throughout, which is precisely what makes the row count insufficient.
    """
    class _W4Ctx(_MockCtx):
        def client_child(self, tag, mode, port, payload, start_barrier=None,
                         http_timeout=120.0):
            # The non-proxy cycle reads the result off the child's meta, the
            # way the real client_child leaves it there.
            child = _MockChild(f"client_{tag}")
            child.meta = {"client_result": {"bytes_received": 0,
                                             "got_http_ack": False}}
            self.children.append({"tag": tag, "role": "child-client"})
            return child

    ctx = _W4Ctx(tmp_path)
    monkeypatch.setattr(H, "DirectionProxy", _MockProxy)
    monkeypatch.setattr(H, "spawn_child",
                        lambda role, args, env, workdir, stderr,
                        registry=None: _MockChild("client_w4"))
    monkeypatch.setattr(H, "identity_readback",
                        lambda target, host, session, event: {
                            "identity": {"host": host, "session_id": session,
                                         "event_id": event},
                            "row_count": 1, "rows": []})
    monkeypatch.setattr(H, "wait_identity_count",
                        lambda target, host, session, event, want,
                        timeout=420.0: 1)
    monkeypatch.setattr(H, "post_event",
                        lambda port, payload, timeout=120.0: (status, ack))
    monkeypatch.setattr(H, "marker_state",
                        lambda root, host, session, event: {
                            "identity": {"host": host, "session_id": session,
                                         "event_id": event},
                            "identity_hash": "x", "host_namespace": "namespaced",
                            "expected_accepted_path": "p",
                            "accepted_present": True, "accepted_files": [],
                            "pending_present": False, "pending_files": [],
                            "converged": True})
    monkeypatch.setattr(H, "qa_rows_for_session", lambda target, session: 1)

    result = H.run_w4(ctx)
    assert result["cycles_completed"] == 3, "all three cycles must still run"
    assert all(c["rows_after_recovery"] == 1 for c in result["cycles"]), \
        "the row count is 1 in every case — it is not what decides"
    flagged = any("recovery was not accepted" in p for p in result["problems"])
    assert all(c["recovery_ok"] is (not rejected) for c in result["cycles"])
    assert flagged is rejected, result["problems"]
    assert (result["verdict"] == "FAIL") is rejected, result


def test_w4_is_repeated_crash_recover_not_concurrency():
    """§6 W4 = repeated crash/recover >= 3. Concurrency is a different case."""
    import inspect
    src = inspect.getsource(H.run_w4)
    assert "cycles_required = 3" in src
    assert "for index in range(cycles_required)" in src
    assert "crash_clean" in src
    assert "only {len(cycles)} of {cycles_required}" in src, (
        "W4 must fail when fewer than 3 cycles ran")
    # The retired concurrency shape must not come back.
    assert "--start-barrier" not in src
    assert "w4_0" not in src or "w4_{index}" in src
    assert "concurrent" not in src.lower()


def test_section6_conditions_are_evaluated_and_gated_not_just_narrated():
    """The six §6 conditions must be part of the acceptance RULE."""
    import inspect
    for name, fn in H.W_CASES.items():
        src = inspect.getsource(fn)
        assert "evaluate_case_section6" in src, (
            f"{name} does not evaluate the mission §6 end-state conditions")
    # ...and evaluate_acceptance must actually reject on them.
    ev = _passing_evidence(cases=H.FULL_MATRIX)
    assert H.evaluate_acceptance(ev)["verdict"] == "PASS"
    ev["steps"]["W1"]["section6"] = {
        "case": "W1", "section6_ok": False,
        "section6_problems": ["transport did not converge: pending survives"],
        "checks": {"transport_converged": False}}
    rejected = H.evaluate_acceptance(ev)
    assert rejected["verdict"] == "FAIL", (
        "a §6 failure must reject the run even with every W case PASS")
    assert rejected["exit_code"] == H.EXIT_REJECTED
    assert any("§6" in line for line in rejected["failures"])


def test_a_case_without_a_section6_result_is_rejected():
    """A case that never evaluated §6 is missing proof, not a pass."""
    ev = _passing_evidence(cases=H.FULL_MATRIX)
    del ev["steps"]["W4"]["section6"]
    result = H.evaluate_acceptance(ev)
    assert result["verdict"] == "FAIL"
    assert any("section6" in line for line in result["failures"])


def test_classify_marker_phase_names_the_three_observable_states():
    """Pure: the three windows must be named, not re-derived per call site."""
    assert H.classify_marker_phase(False, 0) == "pre_commit"
    assert H.classify_marker_phase(False, 1) == "pre_tombstone"
    assert H.classify_marker_phase(True, 1) == "converged"


def test_marker_state_does_not_let_a_legacy_marker_vouch_for_a_real_host():
    """Host scoping is part of the PATH; a glob would collapse it."""
    import hashlib
    import json
    import tempfile
    root = Path(tempfile.mkdtemp())
    acc = root / "profile" / "j" / "accepted_live_buffer"
    acc.mkdir(parents=True)
    ident = hashlib.sha256(b"S\x00E").hexdigest()
    (acc / f"{ident}.json").write_text(json.dumps(
        {"session_id": "S", "msg_id": "E", "accepted": True, "host": ""}),
        encoding="utf-8")
    legacy = H.marker_state(root, "", "S", "E")
    assert legacy["accepted_present"] is True
    assert legacy["host_namespace"] == "legacy"
    real = H.marker_state(root, "other-host", "S", "E")
    assert real["accepted_present"] is False, (
        "a legacy-namespace marker must not satisfy a real-host lookup")
    assert real["converged"] is False


def test_marker_state_reports_a_surviving_pending_item_as_not_converged():
    import hashlib
    import json
    import tempfile
    root = Path(tempfile.mkdtemp())
    acc = root / "profile" / "j" / "accepted_live_buffer" / "h"
    acc.mkdir(parents=True)
    ident = hashlib.sha256(b"S\x00E").hexdigest()
    (acc / f"{ident}.json").write_text("{}", encoding="utf-8")
    pending = root / "profile" / "j" / "pending_live_buffer"
    pending.mkdir(parents=True)
    (pending / "j1.json").write_text(json.dumps(
        {"session_id": "S", "msg_id": "E", "host": "h"}), encoding="utf-8")
    state = H.marker_state(root, "h", "S", "E")
    assert state["accepted_present"] is True
    assert state["pending_present"] is True
    assert state["converged"] is False
    # A pending marker for a DIFFERENT identity must not count against this one.
    other = root / "profile" / "j" / "pending_live_buffer2"
    other.mkdir(parents=True)
    (other / "j2.json").write_text(json.dumps(
        {"session_id": "S", "msg_id": "OTHER", "host": "h"}), encoding="utf-8")
    (pending / "j1.json").unlink()
    assert H.marker_state(root, "h", "S", "E")["converged"] is True


def test_section6_gate_catches_each_individual_condition():
    import tempfile
    root = Path(tempfile.mkdtemp())
    good = {"accepted_present": True, "accepted_files": [],
            "pending_present": False, "pending_files": [], "converged": True}
    assert H.evaluate_case_section6(
        "W1", identity_rows=1, duplicate_qa_rows=1, marker=good,
        root=root)["section6_ok"] is True
    assert H.evaluate_case_section6(
        "W1", identity_rows=2, duplicate_qa_rows=1, marker=good,
        root=root)["section6_ok"] is False          # not 1 canonical row
    assert H.evaluate_case_section6(
        "W1", identity_rows=1, duplicate_qa_rows=2, marker=good,
        root=root)["section6_ok"] is False          # duplicate QA
    assert H.evaluate_case_section6(
        "W1", identity_rows=1, duplicate_qa_rows=1, marker=None,
        root=root)["section6_ok"] is False          # never read back
    # A marker resolved OUTSIDE the isolated root is a foreign data root.
    foreign = dict(good, accepted_files=["C:/somewhere/else/x.json"])
    assert H.evaluate_case_section6(
        "W1", identity_rows=1, duplicate_qa_rows=1, marker=foreign,
        root=root)["section6_ok"] is False


def test_w0_and_w3_use_opposite_barrier_phases():
    """The distinction is asserted in code, not just described."""
    import inspect
    w0, w3 = inspect.getsource(H.run_w0), inspect.getsource(H.run_w3)
    assert "classify_barrier_phase" in w0 and "before_commit" in w0
    assert "classify_barrier_phase" in w3 and "after_commit" in w3
    # W0 arms the pre-commit barrier, so the count it reads at the barrier
    # must be zero. W3 arms NO barrier: it waits for the real commit and then
    # reads the count, which must be one. The two cases therefore cannot be
    # one test wearing two labels.
    assert "barrier=barrier" in w0
    assert "barrier=" not in w3
    assert "wait_identity_count" in w3
    assert 'at_barrier["row_count"] != 0' in w0
    assert 'committed["row_count"] != 1' in w3


# ══════════════════════════════════════════════════════════════════════════
# ACK vocabulary the harness asserts on must match the product contract
# ══════════════════════════════════════════════════════════════════════════
def test_ack_vocabulary_the_harness_asserts_on_is_the_product_vocabulary():
    from v3core.bridge_contract import ack_from_receipt
    accepted = ack_from_receipt({"status": "accepted", "event_id": "e", "host": "h"})
    assert accepted["ok"] is True and accepted["accepted"] is True
    assert accepted["duplicate"] is False
    duplicate = ack_from_receipt({"status": "duplicate", "event_id": "e", "host": "h"})
    assert duplicate["duplicate"] is True and duplicate["accepted"] is False
    # W1/W2/W4 assert accepted; W3 asserts duplicate. Both must be reachable,
    # and a write that did not become durable must never read as success.
    failed = ack_from_receipt({"status": "failed", "event_id": "e", "host": "h"})
    assert failed["ok"] is False and failed["accepted"] is False


def test_every_w_case_uses_the_real_events_endpoint():
    import inspect
    for name, fn in H.W_CASES.items():
        source = inspect.getsource(fn)
        assert ("post_event" in source or "child-client" in source
                or "raw_post_through_proxy" in source), \
            f"{name} must drive a real HTTP /events request"
    # The in-process POST helper is the /events route, not a shortcut.
    assert "/events" in inspect.getsource(H.post_event)
    assert "/events" in inspect.getsource(H.raw_post_through_proxy)
    # ...and so is every client-child mode, including the fault modes.
    parser = H.build_parser()
    for mode in ("deliver", "no-ack", "fail-reset", "fail-timeout"):
        args = parser.parse_args(["child-client", "--mode", mode, "--port", "1",
                                  "--payload", "{}"])
        assert args.mode == mode
    assert "/events" in inspect.getsource(H._run_client_child)


# ══════════════════════════════════════════════════════════════════════════
# f2-04 W4: the child-launch handshake and the result-drain race
#
# PID3356 exited before ``process_identity`` could read its live OS triple:
# ``Popen`` returns immediately, the PowerShell inventory costs seconds, and a
# fast child-client (an immediate duplicate POST) finishes and exits inside
# that window. The repair is a deterministic handshake AT the spawn boundary,
# plus a ``wait_event`` that no longer reports a queued result as missing.
#
# Bounded, fake-proc and process-free: no child is spawned, no identity is
# ever faked into existence, and every filesystem touch is a ``tmp_path``.
# ══════════════════════════════════════════════════════════════════════════
class _HandshakeProc:
    """A ``Popen`` stand-in whose child is a scripted script, in-process.

    ``stdout`` is a real pipe, and the script is executed by a thread that
    plays the child. The child models the REAL ``_run_client_child`` contract
    rather than a caricature of it:

    * if the argv it was handed carries ``--identity-release`` (the real
      child called :func:`H.await_identity_release`), it parks on that path and
      does NOTHING else until the parent publishes the release;
    * if the argv carries no such flag — the pre-fix world — it runs its
      workload immediately and exits, which is precisely the f2-04 PID3356
      failure (a child that finishes its duplicate POST while the parent is
      still inside the slow OS identity capture).

    That distinction is what makes this regression BITE: a fake that always
    parked would make the assertion pass whether or not the handshake exists.
    """

    def __init__(self, script, pid: int = 5150) -> None:
        self.pid = pid
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self._alive = True
        self.released = threading.Event()
        self.write_child_start = True
        self.emit_result = True
        self.on_identity_seen = None          # called while the child parks
        self._thread: threading.Thread | None = None
        self._make = script
        self.argv: list[str] = []

    def _install_pipe(self):
        r, w = os.pipe()
        # closefd=False: these wrappers must not own (or close) the fds, and
        # line buffering so ``child_start`` is visible to the reader thread
        # immediately — the whole point of the handshake.
        self.stdout = open(r, "r", buffering=1, encoding="utf-8",
                           errors="replace", closefd=False)
        self._read_fd = r
        self._write_fd = w
        return open(w, "w", buffering=1, encoding="utf-8", closefd=False)

    def note_argv(self, argv) -> None:
        """Record the argv the parent actually built, BEFORE Popen returns."""
        self.argv = [str(a) for a in argv]

    def _popen(self, argv, **kwargs):
        self.note_argv(argv)
        return self

    def run(self, release: Path, on_identity_seen=None, identity_timeout=1.0):
        """Start the scripted child. The test drives the parent side."""
        out = self._install_pipe()
        self._script_thread(release, out, on_identity_seen, identity_timeout)
        return self

    def _script_thread(self, release: Path, out, on_identity_seen,
                       identity_timeout: float) -> None:
        # Does this child know it is gated? The real child reads exactly this
        # off its own argv.
        gated = "--identity-release" in self.argv
        release_arg = ""
        if gated:
            idx = self.argv.index("--identity-release")
            release_arg = self.argv[idx + 1]
        gate_path = Path(release_arg) if release_arg else release

        def _body() -> None:
            try:
                if self.write_child_start:
                    out.write(json.dumps({"event": "child_start",
                                          "role": "child-client",
                                          "pid": self.pid,
                                          "identity_release": release_arg}) + "\n")
                    out.flush()
                if on_identity_seen is not None:
                    on_identity_seen()
                if gated:
                    # The park: NOTHING else until the parent releases.
                    deadline = time.time() + identity_timeout
                    while time.time() < deadline and not gate_path.exists():
                        time.sleep(0.005)
                    self.released.set()
                # An ungated child goes straight to its workload and exits —
                # the f2-04 race, reproduced on demand.
                if self.emit_result and (not gated or gate_path.exists()):
                    out.write(json.dumps({"event": "client_result",
                                          "role": "child-client",
                                          "pid": self.pid,
                                          "outcome": "delivered"}) + "\n")
                    out.flush()
            finally:
                self._alive = False
                try:
                    out.close()
                except Exception:
                    pass
                # Closing the write end is what lets the reader see EOF and
                # finish, so the drain in wait_event can actually complete.
                for fd in (getattr(self, "_write_fd", None),):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        self._write_fd = None
        self._thread = threading.Thread(target=_body, daemon=True)
        self._thread.start()

    def poll(self):
        if self._alive:
            return None
        self.returncode = 0
        return 0

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._alive = False
        self.returncode = 0
        return 0

    def close_stderr(self):
        pass


def test_child_client_launch_handshake_parks_before_workload_and_fails_closed(
        monkeypatch, tmp_path):
    """REGRESSION: the child cannot do work before its identity is captured.

    f2-04 W4 lost PID3356 to the gap between ``Popen`` and ``process_identity``.
    The fix is a handshake at the spawn boundary, and this asserts both halves
    of it: the child parks until the parent has a REAL identity triple and has
    registered it, and the release only appears after that. A stale release
    file from a previous run must not open the gate early, and the
    ``--start-barrier`` workload gate must remain a separate, intact flag.
    """
    root = tmp_path / "run"
    (root / "logs").mkdir(parents=True)
    (root / "barriers").mkdir(parents=True)
    stderr_path = root / "logs" / "client_w4_0.stderr.log"
    stderr_path.write_text("", encoding="utf-8")

    # A release file left behind by an earlier run: it must NOT release this
    # child. The release path is unique PER SPAWN (fresh UUID suffix), so an
    # earlier run's file is not merely stale — it is a different path. The
    # label is derived exactly as spawn_child derives it.
    label = f"child-client.{Path(stderr_path).stem}"
    stale = H.identity_release_path(root, "OLD-NONCE", label, "OLD-SPAWNID")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("old-spawn-id", encoding="utf-8")
    assert stale.exists(), "the stale release must exist to be a real trap"

    observed: dict = {}

    def _fake_identity(pid):
        # Fires while the child is parked and BEFORE the parent has captured
        # anything. If the child were allowed to work at this point, f2-04
        # would recur. Reads the proc out of ``box`` because the callback can
        # fire before any local ``proc`` name is bound.
        p = box.get("proc")
        observed["at_identity_time"] = {
            "proc_returncode": p.poll() if p is not None else None,
            "release_exists": Path(box["release_path"]).exists(),
            "stale_exists": stale.exists(),
            "gated": "--identity-release" in (p.argv if p is not None else []),
        }
        return {"pid": pid, "ppid": 77, "created": "2026-10-02T00:00:00.000Z",
                "cmdline": f"python -m eval.f2_source_idempotency_e2e {pid}"}

    # The fake is built BY the Popen shim, so it sees the argv the harness
    # really assembled — that is what decides whether it parks, and the
    # per-spawn release path is only knowable from that argv.
    box: dict = {}

    def _fake_popen(argv, **kwargs):
        p = _HandshakeProc(None)
        p.note_argv(argv)          # the harness's own child_argv(...)
        box["proc"] = p
        # The parent minted a per-spawn path; capture it exactly as a real
        # child reads it off its own argv.
        argv_s = [str(a) for a in argv]
        idx = argv_s.index("--identity-release")
        box["release_path"] = argv_s[idx + 1]
        # Start the scripted child only after the pipe exists. _HandshakeProc
        # parks on the argv path itself, which is this spawn's real path.
        p.run(Path(box["release_path"]),
              on_identity_seen=lambda: _fake_identity(p.pid),
              identity_timeout=3.0)
        return p

    # ``H.subprocess`` IS the stdlib module, so patching ``Popen`` on it would
    # be global. Proxy the module's own attribute instead: everything forwards,
    # only Popen is the fake, and monkeypatch restores the real module. Popen
    # is an INSTANCE attribute — as a class attribute it would bind as a
    # method and swallow the first real argument.
    import subprocess as _RealSubprocess

    class _SubShim:
        def __init__(self, popen):
            self.Popen = popen

        def __getattr__(self, name):
            return getattr(_RealSubprocess, name)

    monkeypatch.setattr(H, "subprocess", _SubShim(_fake_popen))
    monkeypatch.setattr(H, "process_identity", _fake_identity)
    monkeypatch.setattr(H, "_default_killer", lambda pid: {"rc": 0})
    monkeypatch.setattr(H, "IDENTITY_START_TIMEOUT", 5.0)

    registry = H.ChildRegistry(lookup=lambda pid: None)
    child = H.spawn_child("child-client",
                          ["--mode", "deliver", "--port", "1", "--payload", "{}"],
                          env={}, workdir=tmp_path, stderr_path=stderr_path,
                          registry=registry)
    proc = box["proc"]

    # The parent captured a REAL live OS triple — never Popen metadata, never
    # a placeholder that survived.
    assert child.identity["pid"] == proc.pid
    assert child.identity["created"] == "2026-10-02T00:00:00.000Z"
    assert child.identity["ppid"] == 77
    assert "identity_pending" not in child.identity, (
        "a placeholder identity must never survive the handshake")
    assert "f2-source-idempotency" in str(child.identity.get("marker"))

    # Registration happened before the release was published.
    assert registry._children == [child]

    # The child only ran after the release; at identity time the release did
    # not exist and the process had not exited.
    assert observed["at_identity_time"]["release_exists"] is False
    assert observed["at_identity_time"]["proc_returncode"] is None
    # ...and the STALE file from the earlier run was never what opened it.
    assert observed["at_identity_time"]["stale_exists"] is True
    assert child.meta["identity_handshake"]["gated"] is True
    assert child.meta["identity_handshake"]["child_start_seen"] is True
    # The release path is UNIQUE PER SPAWN: it carries the per-run nonce, the
    # per-tag label AND a fresh spawn id, so no other run's or other spawn's
    # file can ever be at this path.
    assert child.meta["identity_handshake"]["release_path"] != str(stale)
    spawn_id = child.meta["identity_handshake"]["release_spawn_id"]
    assert spawn_id and spawn_id in child.meta["identity_handshake"]["release_path"]
    assert child.meta["identity_handshake"]["release_path"] == box["release_path"]
    # Two spawns of the same tag in the same run must not share a path.
    assert H.identity_release_path(root, H._RUN_NONCE, label,
                                   H._mint_identity_spawn_id()) != \
        H.identity_release_path(root, H._RUN_NONCE, label,
                                H._mint_identity_spawn_id())
    # A STALE file sitting at a would-be release path must not open the gate:
    # the parent clears the path before the child can observe it. Exercised
    # on a scratch path so this spawn's own published release is untouched.
    would_be = H.identity_release_path(root, H._RUN_NONCE, label,
                                       H._mint_identity_spawn_id())
    would_be.parent.mkdir(parents=True, exist_ok=True)
    would_be.write_text("left over from a previous spawn", encoding="utf-8")
    assert would_be.exists()
    H._clear_stale_release(would_be)
    assert not would_be.exists(), (
        "a stale release at a spawn's own path must be cleared before the "
        "child can observe it")

    # The release was written, and the parked child then completed its result.
    assert proc.released.wait(2.0) is True
    release = Path(child.meta["identity_handshake"]["release_path"])
    assert release.exists() and release.read_text(encoding="utf-8") == spawn_id
    assert child.wait_event("client_result", timeout=5.0)["outcome"] == "delivered"

    # The workload gate is still a DISTINCT, preserved flag, and the child argv
    # carries the handshake without disturbing it.
    argv = child.meta["argv_args"]
    assert "--identity-release" in argv and "--identity-timeout" in argv
    parser = H.build_parser()
    parsed = parser.parse_args(["child-client", "--mode", "deliver", "--port", "1",
                                "--payload", "{}", "--start-barrier", "C:/m"])
    assert parsed.start_barrier == "C:/m"          # preserved, not repurposed
    assert parsed.identity_release == ""           # distinct flag

    # ORDER, asserted on the source: a FAILED identity handshake must be
    # refused BEFORE the workload gate is waited on. Waiting out a
    # ``--start-barrier`` for a child whose identity the parent never
    # verified burns the gate's whole timeout and then runs the workload
    # anyway. A child that DID complete its handshake still reaches the gate.
    import inspect
    spawn_src = inspect.getsource(H.spawn_child)
    assert 'gated = role == "child-client"' in spawn_src, (
        "child-serve is NOT handshake-gated: it blocks in serve() for the "
        "whole run, so its window is already non-empty. Gating it would park "
        "a process that must never be released until it is registered, for no "
        "gain.")
    assert "_clear_stale_release(release)" in spawn_src, (
        "spawn_child must clear a stale release at the path before the child "
        "can observe it — existence has to be caused by THIS spawn")
    assert spawn_src.index("_clear_stale_release(release)") < \
        spawn_src.index("subprocess.Popen("), (
        "the stale release must be cleared BEFORE the child exists")
    client_src = inspect.getsource(H._run_client_child)
    refuse_at = client_src.index('if identity_note.get("released") is False')
    gate_at = client_src.index("if args.start_barrier:")
    payload_at = client_src.index("payload = json.loads(args.payload)")
    assert refuse_at < gate_at, (
        "a failed identity handshake must be refused before the workload gate")
    assert refuse_at < payload_at, (
        "a failed identity handshake must be refused before the payload is "
        "parsed and before any socket is opened")
    # The gate itself is intact and still reachable for a released child.
    assert "start-barrier-timeout" in client_src

    # HONESTY: the release is an existence gate, and the documentation must
    # not claim otherwise. An earlier version asserted content-token
    # authentication and atomic writes that no code enforced, and
    # ``await_identity_release`` never read the content at all. The
    # documentation must therefore SAY SO (a bare ``authenticates`` substring
    # test cannot tell a claim from its denial, so the honest statements are
    # asserted positively, on whitespace-normalised text because a sentence
    # may wrap across a line), and the child must genuinely never read it.
    def _norm_doc(fn) -> str:
        return " ".join((fn.__doc__ or "").lower().split())

    release_doc = _norm_doc(H.identity_release_path)
    assert "existence gate" in release_doc, (
        "identity_release_path must state that the release is an existence "
        "gate, not an authenticated token")
    assert "authenticates anything, and it does not" in release_doc
    writer_doc = _norm_doc(H._identity_release_file)
    assert "not an authentication token" in writer_doc, (
        "_identity_release_file must not claim its write is an authentication "
        "or atomicity mechanism")
    assert "not claimed to be atomic" in writer_doc
    release_src = inspect.getsource(H.await_identity_release)
    assert "read_text" not in release_src, (
        "the release is an EXISTENCE gate: the child must never read the "
        "file's content")


def test_spawn_child_refuses_when_the_os_reports_no_identity(monkeypatch, tmp_path):
    """Fail-closed: no identity, no release, no OwnedChild.

    This is the branch the repair must NOT weaken into a pass. If the OS has
    no such process, the spawn raises and the child is never released to run
    its workload, and the release file is never published.

    It is also the one path where NO verified identity exists, so cleanup must
    use the parent's OWN Popen handle (``terminate``/``wait``) and must NOT
    fall back to a bare-PID tree kill: there is no verified identity to
    re-check, and the child was never released to any workload. That is
    asserted directly — the module-level killer is instrumented and must
    never be called on this path.

    The un-gated ``child-serve`` branch keeps the original f2-04 lifecycle:
    capture the live identity FIRST, then ``start()``/register. It needs no
    pre-identity pump placeholder (serve() blocks for the whole run, so its
    window is already non-empty), and it is gated on no release path at all —
    asserted here so the two branches cannot quietly converge.
    """
    import inspect
    spawn_src = inspect.getsource(H.spawn_child)
    un_gated = spawn_src[spawn_src.index("        else:",
                                         spawn_src.index("if gated:")):]
    identity_at = un_gated.index("process_identity(proc.pid)")
    start_at = un_gated.index("child.start(Path(stderr_path))")
    register_at = un_gated.index("registry.add(child)")
    assert identity_at < start_at < register_at, (
        "child-serve must capture its live identity BEFORE start/register — "
        "a pre-identity pump placeholder is the f2-04 regression")
    assert "_start_pump" not in un_gated, (
        "child-serve needs no pre-identity pump: it blocks in serve() for the "
        "whole run, so its window is already non-empty")

    root = tmp_path / "run"
    (root / "logs").mkdir(parents=True)
    (root / "barriers").mkdir(parents=True)
    stderr_path = root / "logs" / "client_w0.stderr.log"
    stderr_path.write_text("", encoding="utf-8")

    box: dict = {}
    killed: list[int] = []

    def _fake_popen(argv, **kwargs):
        p = _HandshakeProc(None)
        p.note_argv(argv)
        box["proc"] = p
        argv_s = [str(a) for a in argv]
        box["release_path"] = argv_s[argv_s.index("--identity-release") + 1]
        p.run(Path(box["release_path"]), identity_timeout=0.2)
        return p

    import subprocess as _RealSubprocess

    class _SubShim:
        def __init__(self, popen):
            self.Popen = popen

        def __getattr__(self, name):
            return getattr(_RealSubprocess, name)

    monkeypatch.setattr(H, "subprocess", _SubShim(_fake_popen))
    monkeypatch.setattr(H, "process_identity", lambda pid: None)
    # The REAL _default_killer, instrumented: a bare-PID kill on this path is
    # the exact violation being guarded, so it must never fire.
    monkeypatch.setattr(H, "_default_killer",
                        lambda pid: (killed.append(int(pid)),
                                     {"rc": 0, "out": "", "err": ""})[1])
    monkeypatch.setattr(H, "IDENTITY_START_TIMEOUT", 5.0)

    with pytest.raises(H.F2HarnessError) as excinfo:
        H.spawn_child("child-client",
                      ["--mode", "deliver", "--port", "1", "--payload", "{}"],
                      env={}, workdir=tmp_path, stderr_path=stderr_path,
                      registry=H.ChildRegistry(lookup=lambda pid: None))
    msg = str(excinfo.value)
    assert "no such process" in msg and "verifiable identity" in msg
    # The release was NOT published: the child could not proceed unverified.
    release = Path(box["release_path"])
    assert not release.exists(), (
        "a spawn that could not verify an identity must not release the child")
    # No bare-PID kill: the unverified child was cleaned through the parent's
    # own Popen handle (terminate/wait), which is the only thing it owns.
    assert killed == [], (
        f"a PID-based kill ran with no verified identity: {killed}")
    assert H._default_killer is not None


def test_release_publish_failure_cleans_the_verified_child_and_stays_hard(
        monkeypatch, tmp_path):
    """REGRESSION: a release-publish failure is not a silently parked orphan.

    The release write is the LAST step of the protected startup, after the
    identity is captured and the OwnedChild is REGISTERED. So when it fails,
    there is already a verified owned child — and it must be cleaned through
    the ordinary verified route (``OwnedChild.stop`` → ``terminate_owned``,
    which re-reads the triple and tree-kills), with no bare-PID kill and no
    removal from the registry before it is proven dead. The failure itself
    must propagate: a release-publish failure is a hard error, never a
    silently parked child.
    """
    root = tmp_path / "run"
    (root / "logs").mkdir(parents=True)
    (root / "barriers").mkdir(parents=True)
    stderr_path = root / "logs" / "client_w0_releasefail.stderr.log"
    stderr_path.write_text("", encoding="utf-8")

    box: dict = {}

    def _fake_popen(argv, **kwargs):
        p = _HandshakeProc(None)
        p.note_argv(argv)
        box["proc"] = p
        argv_s = [str(a) for a in argv]
        box["release_path"] = argv_s[argv_s.index("--identity-release") + 1]
        p.run(Path(box["release_path"]), identity_timeout=0.2)
        return p

    import subprocess as _RealSubprocess

    class _SubShim:
        def __init__(self, popen):
            self.Popen = popen

        def __getattr__(self, name):
            return getattr(_RealSubprocess, name)

    # A live, verified identity for the child, backed by a fake process table
    # so the ordinary stop() route can re-verify and clean it. ``registry.add``
    # installs the registry's OWN killer onto the child, so that is the
    # function to instrument: reaching it PROVES cleanup went through the
    # verified route, and the module-level ``_default_killer`` staying silent
    # proves no unverified PID-based tree kill was used instead.
    table = FakeTable()
    verified_kills: list[int] = []
    bare_kills: list[int] = []

    def _verified_kill(pid):
        verified_kills.append(int(pid))
        return table.kill(pid)

    def _fake_identity(pid):
        table.rows[int(pid)] = _identity(pid)
        return dict(_identity(pid))

    monkeypatch.setattr(H, "subprocess", _SubShim(_fake_popen))
    monkeypatch.setattr(H, "process_identity", _fake_identity)
    monkeypatch.setattr(H, "_default_killer",
                        lambda pid: (bare_kills.append(int(pid)),
                                     table.kill(pid))[1])
    monkeypatch.setattr(H, "IDENTITY_START_TIMEOUT", 5.0)
    # Make the publish itself fail, AFTER registration.
    monkeypatch.setattr(H, "_identity_release_file",
                        lambda path, spawn_id: (_ for _ in ()).throw(
                            OSError("publish refused by test")))

    registry = H.ChildRegistry(lookup=table.lookup, killer=_verified_kill)
    with pytest.raises(OSError) as excinfo:
        H.spawn_child("child-client",
                      ["--mode", "deliver", "--port", "1", "--payload", "{}"],
                      env={}, workdir=tmp_path, stderr_path=stderr_path,
                      registry=registry)
    assert "publish refused by test" in str(excinfo.value)

    # The child WAS registered (it was verified before the publish), and the
    # verified route cleaned it: the fake table is now empty.
    pid = box["proc"].pid
    assert table.lookup(pid) is None, (
        "a release-publish failure must still clean the verified owned child")
    assert verified_kills == [pid], (
        "cleanup must go through the verified route (terminate_owned, which "
        f"re-reads the identity triple): {verified_kills}")
    assert bare_kills == [], (
        f"a bare-PID tree kill ran instead of the verified route: {bare_kills}")
    # ...and it was NOT silently removed from the registry before being proven
    # dead: the child is still tracked there, so a cleanup fault would surface.
    assert len(registry._children) == 1
    assert registry._children[0].identity["pid"] == pid
    assert registry._children[0].stopped is True, (
        "the child stays tracked until the verified stop has proven it dead")
    # No release file was published on the failing path.
    assert not Path(box["release_path"]).exists()


def test_wait_event_drains_a_queued_result_before_calling_it_missing():
    """REGRESSION: an exited child whose reader is still draining.

    ``wait_event`` used to raise the moment it saw ``poll() is not None``,
    so a result already written to the pipe but not yet parsed was reported as
    MISSING. The fix joins the reader once, bounded, OUTSIDE the lock, then
    re-checks. The genuinely-missing case must still be an F2HarnessError, and
    a stopped child must stay silent.

    The race leg drives the REAL ``_drain_reader``: ``child2._reader`` is the
    actual thread, the coordination hook is used ONLY to release that thread
    at the moment the real drain begins its join, and the appends are done by
    the thread itself. Replacing ``_drain_reader`` with a stand-in (or leaving
    ``_reader`` unset, which makes the real drain a no-op that returns True
    before the thread has run at all) would pass with the join never executed,
    so both are asserted to be in play.
    """
    # (1) Genuinely missing: exited, reader finished, no such event.
    child = H.OwnedChild("child-serve", FakeProc(9001, returncode=3),
                         _identity(9001))
    child._lines = ["partial output", "no event here"]
    with pytest.raises(H.F2HarnessError) as excinfo:
        child.wait_event("client_result", timeout=0.2)
    assert "exited" in str(excinfo.value) and "rc=3" in str(excinfo.value)

    # (2) The race: the process has exited, but the reader thread appends the
    # queued result just after the first poll() observes the exit. The REAL
    # drain must find it by actually joining the REAL thread.
    child2 = H.OwnedChild("child-client", FakeProc(9002, returncode=0),
                          _identity(9002))
    child2._lines = []
    released = threading.Event()
    # The appends happen ON THE THREAD, so they cannot precede the join the
    # real drain performs: this is the assertion that the join is real.
    appended: list[str] = []

    def _late_reader():
        released.wait(5.0)
        with child2._lock:
            child2._lines.append(json.dumps({"event": "client_result",
                                             "outcome": "delivered"}))
            child2.events.append({"event": "client_result",
                                  "outcome": "delivered"})
            appended.append("done")

    thread = threading.Thread(target=_late_reader, daemon=True)
    child2._reader = thread          # the REAL reader the real drain joins
    real_drain = H.OwnedChild._drain_reader
    drain_calls: list[float] = []

    def _drain(remaining=5.0):
        # Coordination hook ONLY: release the parked reader the instant the
        # REAL join starts, then delegate to the real implementation. The
        # join, the lock discipline and the liveness result are all the
        # original's — nothing here appends, finds, or decides anything.
        drain_calls.append(remaining)
        released.set()
        return real_drain(child2, remaining=remaining)

    child2._drain_reader = _drain
    thread.start()
    got = child2.wait_event("client_result", timeout=10.0)
    assert drain_calls, "the drain hook must have been reached"
    assert appended == ["done"], (
        "the queued result must have been appended BY THE READER THREAD the "
        "real drain joined, not by the drain itself")
    assert not thread.is_alive(), "the real bounded join must have finished the reader"
    assert got["outcome"] == "delivered", (
        "a queued result must be drained, not reported missing")

    # (3) A stopped child stays silent: the suppression is unchanged.
    child3 = H.OwnedChild("child-serve", FakeProc(9003, returncode=0),
                          _identity(9003))
    child3.stopped = True
    with pytest.raises(H.F2HarnessError) as excinfo:
        child3.wait_event("never_emitted", timeout=0.2)
    assert "never emitted" in str(excinfo.value)
    assert "exited" not in str(excinfo.value)
