from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from v3core import config_cli as _config_cli
from v3core import config_control as _cc
from v3core import __main__ as _v3core_main
from v3core.config_control import (
    ConfigControlService,
    ConfigValidationError,
    normalize_secret_input,
)


FAKE_EMBED_KEY = "fake-embed-key"
FAKE_RERANK_KEY = "fake-rerank-key"
FAKE_LLM_KEY = "fake-llm-key"
FAKE_PG_PASSWORD = "fake-pg-password"


def _config_text(*, embed: bool = True, rerank: bool = True, llm: bool = True) -> str:
    parts = [
        "basePath: C:/config-lab",
        "storage:",
        "  pg:",
        "    host: 127.0.0.1",
        "    port: 55453",
        "    database: g5a",
        "    user: g5a",
        f"    password: {FAKE_PG_PASSWORD}",
    ]
    if embed:
        parts += [
            "  embed:",
            "    endpoint: http://user:pass@embed.invalid/v1/embeddings?token=secret-query",
            "    model: fake-embed",
            f"    api_key: {FAKE_EMBED_KEY}",
            "    # preserve this comment",
            "    unknown_embed_field: keep-me",
        ]
    if rerank:
        parts += [
            "  rerank:",
            "    endpoint: http://rerank.invalid/v1/rerank",
            "    model: fake-rerank",
            f"    apiKey: {FAKE_RERANK_KEY}",
        ]
    if llm:
        parts += [
            "llm:",
            "  provider: openai",
            "  base_url: http://llm.invalid/v1",
            "  model: fake-llm",
            f"  api_key: {FAKE_LLM_KEY}",
        ]
    parts += ["custom_forward_compatible:", "  keep: yes", ""]
    return "\n".join(parts)


def _write_config(tmp_path: Path, *, crlf: bool = False, **kwargs) -> Path:
    text = _config_text(**kwargs)
    if crlf:
        text = text.replace("\n", "\r\n")
    path = tmp_path / "config.yaml"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_profile_resolution_and_absolute_override(tmp_path, monkeypatch):
    path = _write_config(tmp_path)
    monkeypatch.setenv("V3CORE_CONFIG", str(path))
    service = ConfigControlService(profile="default")
    assert service.path == path
    assert service.validate() == []

    issues = ConfigControlService(profile="default", env={"V3CORE_CONFIG": "relative.yaml"}).validate()
    assert issues and "absolute path" in str(issues[0])


def test_invalid_path_cannot_mutate_fallback(tmp_path):
    service = ConfigControlService(
        profile="default", env={"V3CORE_CONFIG": "relative.yaml"}
    )
    with pytest.raises(ConfigValidationError) as exc:
        service.set_provider("embedding", model="must-not-write")
    assert any(issue.code == "CONFIG_PATH_ERROR" for issue in exc.value.issues)


def test_show_and_json_redact_all_secrets_and_safe_endpoint(tmp_path):
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    human = service.show(redact=False)
    payload = json.loads(service.show(redact=False, as_json=True))
    combined = human + json.dumps(payload)
    for secret in (FAKE_EMBED_KEY, FAKE_RERANK_KEY, FAKE_LLM_KEY, FAKE_PG_PASSWORD, "secret-query"):
        assert secret not in combined
    assert "http://***@embed.invalid/v1/embeddings" in human
    assert payload["db"]["password"] == "CONFIGURED"
    assert payload["providers"]["llm"]["endpoint"] == "CONFIGURED"


def test_validate_reports_malformed_and_control_chars_without_secret(tmp_path):
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("storage:\n  - broken\n", encoding="utf-8")
    issues = ConfigControlService(path=malformed).validate()
    assert any("BAD_SECTION_TYPE" in str(issue) for issue in issues)

    control = tmp_path / "control.yaml"
    raw = _config_text().replace(
        f"api_key: {FAKE_EMBED_KEY}",
        f'apiKey: "before\x16after"',
    )
    control.write_bytes(raw.encode("utf-8"))
    issues = ConfigControlService(path=control).validate()
    rendered = "\n".join(str(issue) for issue in issues)
    assert "C0_CONTROL_CHAR" in rendered
    assert "U+0016" in rendered
    assert "storage.embed.apiKey" in rendered
    assert "before" not in rendered and "after" not in rendered

    other = tmp_path / "other-control.yaml"
    other.write_bytes(_config_text().replace("fake-embed", "fake\x01embed").encode("utf-8"))
    assert any("U+0001" in str(issue) for issue in ConfigControlService(path=other).validate())


def test_secret_normalization_is_strict_and_hidden():
    assert normalize_secret_input("abc\r\n") == "abc"
    assert normalize_secret_input("abc\n") == "abc"
    assert normalize_secret_input("abc\r") == "abc"
    assert normalize_secret_input("abc") == "abc"
    for value in ("", "   ", "abc\tdef", "abc\x16def", "abc\n\n"):
        with pytest.raises(ConfigValidationError) as exc:
            normalize_secret_input(value)
        if value.strip():
            assert value.strip() not in str(exc.value)


def test_atomic_multi_key_preserves_crlf_unknowns_and_failed_input(tmp_path):
    path = _write_config(tmp_path, crlf=True)
    service = ConfigControlService(path=path)
    before_names = {p.name for p in path.parent.iterdir()}
    service.set_keys(["embedding", "rerank"], "new-key")
    with path.open("r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    assert text.count("\r\n") == text.count("\n")
    assert "api_key: new-key" in text and "apiKey: new-key" in text
    assert "unknown_embed_field: keep-me" in text
    assert "preserve this comment" in text
    assert not any(p.name.endswith(('.bak', '.old')) for p in path.parent.iterdir())
    assert {p.name for p in path.parent.iterdir()} - before_names <= {"config.yaml"}

    snapshot = path.read_bytes()
    with pytest.raises(ConfigValidationError):
        service.set_keys(["embedding", "rerank"], "bad\x16key")
    assert path.read_bytes() == snapshot
    with pytest.raises(ConfigValidationError):
        service.set_keys(["embedding", "does-not-exist"], "another")
    assert path.read_bytes() == snapshot
    with pytest.raises(ConfigValidationError):
        service.atomic_update(lambda _text: "storage: [")
    assert path.read_bytes() == snapshot


def test_provider_model_endpoint_and_toggle_updates(tmp_path):
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    service.set_provider("embedding", model="new-embed-model")
    service.set_provider("llm", endpoint="http://llm.invalid/new", model="new-llm")
    service.set_toggle("e1.enabled", False)
    text = path.read_text(encoding="utf-8")
    assert "model: new-embed-model" in text
    assert "base_url: http://llm.invalid/new" in text
    assert "llm.endpoint:" not in text
    assert "model: new-llm" in text
    assert "enabled: false" in text
    with pytest.raises(ConfigValidationError):
        service.set_toggle("not-a-real-toggle", True)


def _run_cli(root: Path, env: dict[str, str], *args: str, input_bytes: bytes | None = None):
    cli_env = dict(env)
    cli_env["PYTHONPATH"] = str(root / "src")
    return subprocess.run(
        [sys.executable, "-m", "v3core", *args],
        cwd=root,
        env=cli_env,
        input=input_bytes,
        capture_output=True,
    )


def test_cli_failure_exit_codes_and_no_key_option(tmp_path):
    root = Path(__file__).parents[1]
    bad = tmp_path / "bad.yaml"
    bad.write_text("storage: [broken]\n", encoding="utf-8")
    env = os.environ.copy()
    env["V3CORE_CONFIG"] = str(bad)
    validate = _run_cli(root, env, "config", "validate")
    assert validate.returncode != 0
    assert b"VALIDATION: FAIL" in validate.stdout

    valid = _write_config(tmp_path, embed=False, rerank=False, llm=False)
    env["V3CORE_CONFIG"] = str(valid)
    unconfigured = _run_cli(root, env, "config", "test", "embedding")
    assert unconfigured.returncode != 0
    assert b"NOT CONFIGURED" in unconfigured.stdout
    invalid_arg = _run_cli(root, env, "config", "set-key", "embedding", "--key", "fake-key")
    assert invalid_arg.returncode != 0


def test_formal_provider_dispatch_is_explicit_and_safe(tmp_path, monkeypatch):
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    fake_cfg = SimpleNamespace(
        embed=SimpleNamespace(endpoint="http://embed.invalid", model="fake-embed"),
        rerank=SimpleNamespace(endpoint="http://rerank.invalid", model="fake-rerank"),
        llm=SimpleNamespace(provider="openai", model="fake-llm", base_url="http://llm.invalid", api_key=FAKE_LLM_KEY),
        pg=SimpleNamespace(host="localhost", port=55453, database="g5a", user="g5a", password=FAKE_PG_PASSWORD),
    )
    monkeypatch.setattr("v3core.config.resolve_config", lambda *args, **kwargs: fake_cfg)
    calls: dict[str, object] = {}
    monkeypatch.setattr("v3core.embedding.build_embed_cfg", lambda cfg: {"endpoint": "http://embed.invalid", "model": "fake-embed"})
    def fake_embedding(text, cfg, **kwargs):
        calls["embedding"] = (text, kwargs)
        return [0.1, 0.2]
    monkeypatch.setattr("v3core.embedding.call_embedding", fake_embedding)
    monkeypatch.setattr("v3core.rerank.rerank", lambda query, docs, cfg: calls.update(rerank=(query, docs)) or [0.9, 0.1])

    class FakeLLM:
        api_key = FAKE_LLM_KEY
        def __init__(self, cfg):
            calls["llm_init"] = True
        def chat(self, system, messages, temperature=0.7):
            calls["llm"] = (system, messages)
            return "OK"
    monkeypatch.setattr("v3core.llm.LLMClient", FakeLLM)

    class Cursor:
        def execute(self, sql):
            calls["sql"] = sql
        def fetchone(self):
            return (1,)
        def close(self):
            pass
    class Conn:
        def cursor(self):
            return Cursor()
        def close(self):
            pass
    monkeypatch.setattr("psycopg2.connect", lambda **kwargs: Conn())

    assert service.test_target("embedding")["ok"]
    assert service.test_target("rerank")["ok"]
    assert service.test_target("llm")["ok"]
    assert service.test_target("postgres")["ok"]
    assert calls["embedding"][0] == "Hippocampus provider validation"
    assert calls["rerank"][0] == "persistent memory"
    assert "ping" in str(calls["llm"])
    assert calls["sql"] == "SELECT 1"
    assert FAKE_LLM_KEY not in json.dumps(calls)


# ---------------------------------------------------------------------------
# Config control plane — focused regression tests for M1 / M2 / M4
# ---------------------------------------------------------------------------


# --- M1: fail-visible config_cli registration -----------------------------


def test_main_registration_failure_uses_safe_err_and_nonzero(monkeypatch, capfd):
    """A config registration failure must be visible and redacted."""
    secret_token = "password123"

    def _explode(_sub):
        raise RuntimeError(
            f"Provider DSN postgresql://u:{secret_token}@h/db failed"
        )

    monkeypatch.setattr(_v3core_main, "add_config_parser", _explode)
    monkeypatch.setattr(sys, "argv", ["v3-core", "config"])

    with pytest.raises(SystemExit) as exc:
        _v3core_main.main()
    assert exc.value.code != 0
    err = capfd.readouterr().err
    assert "config CLI unavailable" in err
    assert secret_token not in err


def test_main_legacy_surface_stays_callable():
    """The hardening must not remove legacy command handlers."""
    assert callable(_v3core_main._run_init_wizard)
    assert callable(_v3core_main._do_migrate)


# --- M2: ConfigControlService.env feeds both show() and resolve_config() --


def test_show_pg_password_status_uses_self_env(monkeypatch, tmp_path):
    """M2(a): ``self.env`` is the authoritative source for the
    ``V3CORE_PG_PASSWORD`` portion of the show() password status when
    it is set; the host ``os.environ`` is ignored in that case so a
    production-leaked process var cannot silently flip status to
    CONFIGURED in a lab profile. When ``self.env is None``, the process
    environment is the fallback (legacy behavior).
    """
    path = _write_config(tmp_path, embed=False, rerank=False, llm=False)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"    password: {FAKE_PG_PASSWORD}\n", ""
        ),
        encoding="utf-8",
    )
    # Lab env says "configured"; host process env says "not configured".
    lab_env = {"V3CORE_PG_PASSWORD": "lab-pw"}
    monkeypatch.delenv("V3CORE_PG_PASSWORD", raising=False)
    service = ConfigControlService(path=path, env=lab_env)
    report = service._build_show_report(*service.read_data(), redact=True)
    assert report["db"]["password"] == "CONFIGURED"

    # Reverse: lab env empty, host env says configured. ``self.env`` is
    # authoritative — status must reflect the lab mapping only.
    monkeypatch.setenv("V3CORE_PG_PASSWORD", "host-pw")
    service = ConfigControlService(path=path, env={"V3CORE_PG_PASSWORD": ""})
    report = service._build_show_report(*service.read_data(), redact=True)
    assert report["db"]["password"] == "NOT CONFIGURED"

    # When ``self.env is None``, the host process value drives status
    # (legacy behavior preserved).
    service_default = ConfigControlService(path=path)
    report_default = service_default._build_show_report(
        *service_default.read_data(), redact=True
    )
    assert report_default["db"]["password"] == "CONFIGURED"


def test_resolve_runtime_cfg_overlays_env_and_restores_process(monkeypatch, tmp_path):
    """M2(b): ``_resolve_runtime_cfg`` must temporarily bind
    ``self.env`` keys (at least ``V3CORE_PG_PASSWORD``) onto
    ``os.environ`` for the duration of ``resolve_config`` and restore
    the exact prior presence/value of every overlaid key in
    ``finally``. ``V3CORE_CONFIG`` bound by ``self.path`` stays
    authoritative — ``self.env["V3CORE_CONFIG"]`` is never allowed to
    override the explicit lab path.
    """
    path = _write_config(tmp_path, embed=False, rerank=False, llm=False)
    fake_cfg = SimpleNamespace(
        embed=None,
        rerank=None,
        llm=None,
        pg=SimpleNamespace(host="lab-host", port=55453, database="g5a", user="g5a", password=""),
    )
    seen_env: dict[str, str | None] = {}

    def fake_resolve(profile, hermes_home=""):
        # Capture the exact ``os.environ`` shape the runtime resolver
        # observed — including every overlaid key and the bound
        # ``V3CORE_CONFIG``.
        seen_env["V3CORE_PG_PASSWORD"] = os.environ.get("V3CORE_PG_PASSWORD")
        seen_env["V3CORE_CONFIG"] = os.environ.get("V3CORE_CONFIG")
        return fake_cfg

    monkeypatch.setattr("v3core.config.resolve_config", fake_resolve)

    # Host process starts with a *different* PG password so we can
    # assert the overlay took effect inside the call and the restore
    # happened on the way out.
    host_pw = "host-leak-pw-token"
    monkeypatch.setenv("V3CORE_PG_PASSWORD", host_pw)
    monkeypatch.delenv("V3CORE_CONFIG", raising=False)

    lab_env = {"V3CORE_PG_PASSWORD": "lab-only-pw", "V3CORE_CONFIG": "/should/be/ignored.yaml"}
    service = ConfigControlService(path=path, env=lab_env)

    # Sanity: nothing has touched the host process environment yet.
    assert os.environ.get("V3CORE_PG_PASSWORD") == host_pw
    assert "V3CORE_CONFIG" not in os.environ

    cfg, issues = _cc._resolve_runtime_cfg(service)
    assert cfg is fake_cfg
    assert issues == []
    # Inside the resolver call, the overlay must have been active.
    assert seen_env["V3CORE_PG_PASSWORD"] == "lab-only-pw"
    # ``self.path`` wins over ``self.env["V3CORE_CONFIG"]``.
    assert seen_env["V3CORE_CONFIG"] == str(path)

    # After the call: process env is restored exactly.
    assert os.environ.get("V3CORE_PG_PASSWORD") == host_pw
    assert "V3CORE_CONFIG" not in os.environ


def test_resolve_runtime_cfg_restores_env_on_resolver_failure(monkeypatch, tmp_path):
    """M2(b) restore-on-error: even when ``resolve_config`` raises, the
    overlay must be reverted to the exact prior presence/value of every
    touched key. No lab values may leak into the caller's process env.
    """
    path = _write_config(tmp_path, embed=False, rerank=False, llm=False)
    monkeypatch.setenv("V3CORE_PG_PASSWORD", "host-pw")
    monkeypatch.delenv("V3CORE_CONFIG", raising=False)

    def boom(profile, hermes_home=""):
        raise RuntimeError("simulated resolve_config failure")

    monkeypatch.setattr("v3core.config.resolve_config", boom)
    service = ConfigControlService(
        path=path,
        env={"V3CORE_PG_PASSWORD": "lab-pw", "OTHER_LAB_KEY": "x"},
    )

    cfg, issues = _cc._resolve_runtime_cfg(service)
    assert cfg is None
    assert any(i.code == "RESOLVE_RUNTIME_FAILED" for i in issues)

    # Exact restore: prior host value back, overlay keys not present.
    assert os.environ.get("V3CORE_PG_PASSWORD") == "host-pw"
    assert "V3CORE_CONFIG" not in os.environ


def test_resolve_runtime_cfg_env_empty_does_not_inherit_host_pw(monkeypatch, tmp_path):
    """M2(b) hardening: when ``self.env`` is explicitly set and a known
    lab-injected key (``V3CORE_PG_PASSWORD``) is absent from it, the
    overlay must temporarily *remove* the host process value. This
    prevents a custom ``env={}`` from accidentally inheriting a
    production PG password through the runtime resolver.
    """
    path = _write_config(tmp_path, embed=False, rerank=False, llm=False)
    seen: dict[str, str | None] = {}

    def fake_resolve(profile, hermes_home=""):
        seen["V3CORE_PG_PASSWORD"] = os.environ.get("V3CORE_PG_PASSWORD")
        return SimpleNamespace(embed=None, rerank=None, llm=None, pg=None)

    monkeypatch.setattr("v3core.config.resolve_config", fake_resolve)
    monkeypatch.setenv("V3CORE_PG_PASSWORD", "host-leak-pw")
    monkeypatch.delenv("V3CORE_CONFIG", raising=False)

    service = ConfigControlService(path=path, env={})
    cfg, _issues = _cc._resolve_runtime_cfg(service)
    assert cfg is not None
    # The resolver saw no PG password — host value was temporarily
    # masked, not inherited.
    assert "V3CORE_PG_PASSWORD" not in seen or seen["V3CORE_PG_PASSWORD"] is None
    # After the call, the host value is back exactly.
    assert os.environ.get("V3CORE_PG_PASSWORD") == "host-leak-pw"


def test_resolve_runtime_cfg_does_not_read_user_home(monkeypatch, tmp_path):
    """M2(b) scope guard: when ``self.path`` is provided, the runtime
    resolver must never consult the user's home / production config.
    ``V3CORE_CONFIG`` is bound to ``self.path`` for the entire call.
    """
    fake_cfg = SimpleNamespace(embed=None, rerank=None, llm=None, pg=None)
    monkeypatch.setattr("v3core.config.resolve_config", lambda *a, **k: fake_cfg)
    monkeypatch.delenv("V3CORE_CONFIG", raising=False)

    # Point HOME at a directory that contains a production-shaped
    # config. If the runtime resolver ever fell through to the home
    # path it would fail to find the lab config — but our guarantee is
    # ``V3CORE_CONFIG`` is pinned to ``self.path`` for the whole call,
    # so the resolver should never look at HOME at all.
    lab_path = _write_config(tmp_path, embed=False, rerank=False, llm=False)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    (fake_home / ".v3-core").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    service = ConfigControlService(path=lab_path, env={})
    cfg, issues = _cc._resolve_runtime_cfg(service)
    assert cfg is fake_cfg
    assert issues == []


# --- M4: unknown readiness token is explicit, not a silent known default -


def _drive_show(service):
    """Run ``_run_show`` with the same shape ``run_config`` uses, but
    without standing up an argparse.Namespace. We invoke the runner
    directly with the smallest possible namespace; the runner reads
    only ``getattr(args, "json", False)`` for our purposes.
    """
    args = SimpleNamespace(json=False)
    return _config_cli._run_show(args, service)


def test_show_known_readiness_tokens_preserved(tmp_path):
    """M4 regression guard: the known readiness tokens must still map
    to the original phrases. ``ok`` → CONFIGURED, ``incomplete`` →
    INCOMPLETE, ``missing_required`` → NOT CONFIGURED,
    ``config_invalid`` / ``unparseable`` → NOT CONFIGURED with the
    distinguishing suffix.
    """
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    # Force the canonical OK path.
    service._readiness = lambda data, issues: "ok"  # type: ignore[assignment]
    rc = _drive_show(service)
    assert rc == 0
    # Drive each known token in turn by patching the show() helper.
    expected = {
        "ok": "READY: CONFIG CONFIGURED (NOT TESTED)",
        "incomplete": "READY: CONFIG INCOMPLETE (NOT TESTED)",
        "missing_required": "READY: NOT CONFIGURED",
        "config_invalid": "READY: NOT CONFIGURED (config invalid)",
        "unparseable": "READY: NOT CONFIGURED (config unparseable)",
    }
    for tok, expected_line in expected.items():
        service._readiness = lambda data, issues, t=tok: t  # type: ignore[assignment]
        captured = []
        orig_emit = _config_cli._emit
        _config_cli._emit = lambda s, _c=captured: _c.append(s)  # type: ignore[assignment]
        try:
            rc = _drive_show(service)
        finally:
            _config_cli._emit = orig_emit  # type: ignore[assignment]
        assert rc == 0
        assert expected_line in captured, (tok, captured)


def test_show_unknown_readiness_token_is_explicit(tmp_path):
    """M4: an unrecognised readiness token must surface as
    ``READY: NOT TESTED (unknown readiness state: '<tok>')`` and
    never silently fall through to a known-looking phrase. We patch
    ``_show_as_text`` to emit an arbitrary fake readiness line.
    """
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    # Drop in a synthetic readiness token that the runner has never
    # been taught to translate. The runner must flag it explicitly.
    def _fake_show_as_text(report, profile, path_, *, redact):
        return f"profile: {profile}\npath:    {path_}\nreadiness: weird-future-state\n"

    # Now drive with a patched ``_show_as_text`` reference inside the
    # closure used by ``_run_show`` — we reach in via the service's
    # ``show`` method by monkey-patching ``_show_as_text`` at module
    # import resolution time.
    import v3core.config_control as _cc
    orig_cc_text = _cc._show_as_text
    _cc._show_as_text = _fake_show_as_text  # type: ignore[assignment]
    orig_emit = _config_cli._emit
    captured2: list[str] = []
    _config_cli._emit = lambda s, _c=captured2: _c.append(s)  # type: ignore[assignment]
    try:
        rc = _drive_show(service)
    finally:
        _cc._show_as_text = orig_cc_text  # type: ignore[assignment]
        _config_cli._emit = orig_emit  # type: ignore[assignment]

    # Even with the unrecognised token, exit code stays 0 if validation
    # passed (the translation is a presentation concern, not a config
    # error). The key assertion is the explicit phrase in the output.
    assert rc == 0
    assert any(
        "NOT TESTED" in line and "unknown readiness state" in line
        and "weird-future-state" in line
        for line in captured2
    ), captured2
    # And it must NOT silently look like a known state.
    assert not any(line == "READY: NOT TESTED" for line in captured2)


def test_show_missing_readiness_line_is_explicit(tmp_path):
    """M4: when ``_show_as_text`` emits no ``readiness:`` line at all,
    the runner must still flag the state explicitly rather than
    emitting a bare ``READY: NOT TESTED``.
    """
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    import v3core.config_control as _cc
    orig_cc_text = _cc._show_as_text

    def _fake_show_as_text(report, profile, path_, *, redact):
        return f"profile: {profile}\npath:    {path_}\n"  # no readiness line

    _cc._show_as_text = _fake_show_as_text  # type: ignore[assignment]
    orig_emit = _config_cli._emit
    captured: list[str] = []
    _config_cli._emit = lambda s, _c=captured: _c.append(s)  # type: ignore[assignment]
    try:
        rc = _drive_show(service)
    finally:
        _cc._show_as_text = orig_cc_text  # type: ignore[assignment]
        _config_cli._emit = orig_emit  # type: ignore[assignment]
    assert rc == 0
    assert any(
        "NOT TESTED" in line and "unknown readiness state" in line
        for line in captured
    ), captured


# ---------------------------------------------------------------------------
# Config control plane — RED regression suite for the global same-name
# YAML key scan defects in the mutation pipeline plus the no-mutation
# semantic round-trip guarantees the production surface must keep.
# Tests use tmp_path + PyYAML (the same parser the runtime uses) and
# never touch the production config, PG, or any external service.
# ---------------------------------------------------------------------------

# Secrets are referenced by reference (variable), never interpolated into the
# expected-text strings, so a leak via assertion-message would still fail the
# substring check below.
_UNIQUE_LEAK_SENTINEL = "config-plane-red-leak-sentinel-VK9m4qL2xZ"  # not printed on purpose


def _read_candidate(path: Path) -> dict | None:
    """Load candidate YAML via PyYAML safe_load (same parser as runtime)."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_candidate_raw(path: Path) -> str:
    """Return raw candidate bytes as text (for line-ending / unknown-field probes)."""
    return path.read_text(encoding="utf-8")


def _embed_secret_round_trip(service, secret: str) -> tuple[dict, str]:
    """Write *secret* to storage.embed and return (parsed, raw)."""
    service.set_keys(["embedding"], secret)
    return _read_candidate(service.path), _read_candidate_raw(service.path)


# --- Requirement 1: set_keys target=embedding must not touch custom.embed ---

def test_set_keys_embedding_does_not_touch_custom_embed_when_sibling_exists(tmp_path):
    """RED: when both ``custom.embed`` and ``storage.embed`` exist in the
    candidate, ``set_keys(['embedding'])`` must mutate ``storage.embed.apiKey``
    only. The current global same-name scan modifies whichever apiKey it
    happens to encounter first, regardless of parent path.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        custom:
          embed:
            apiKey: UNTOUCHED-CUSTOM
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: OLD-STORAGE
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_keys(["embedding"], "NEW-STORAGE")
    parsed = _read_candidate(p)
    # Invariant: custom.embed.apiKey is untouched
    assert parsed["custom"]["embed"]["apiKey"] == "UNTOUCHED-CUSTOM", (
        f"custom.embed.apiKey must remain untouched, got {parsed['custom']['embed']['apiKey']!r}"
    )
    # Invariant: storage.embed.apiKey is the new value
    assert parsed["storage"]["embed"]["apiKey"] == "NEW-STORAGE", (
        f"storage.embed.apiKey must be NEW-STORAGE, got {parsed['storage']['embed']['apiKey']!r}"
    )


def test_set_keys_embedding_with_custom_embed_first_in_file(tmp_path):
    """RED: when ``custom.embed.apiKey`` precedes ``storage.embed.apiKey`` in
    the source order, ``set_keys(['embedding'])`` must still mutate the
    storage.embed.apiKey. The current ``_find_scalar_line`` walks lines
    ignoring parent path, so the first sibling wins.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        custom:
          embed:
            apiKey: UNTOUCHED-CUSTOM
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: OLD-STORAGE
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    before = p.read_bytes()
    svc.set_keys(["embedding"], "FRESH-VALUE")
    parsed = _read_candidate(p)
    assert parsed["custom"]["embed"]["apiKey"] == "UNTOUCHED-CUSTOM", (
        f"custom.embed.apiKey must not be mutated; got {parsed['custom']['embed']['apiKey']!r}"
    )
    assert parsed["storage"]["embed"]["apiKey"] == "FRESH-VALUE", (
        f"storage.embed.apiKey must equal FRESH-VALUE; got {parsed['storage']['embed']['apiKey']!r}"
    )
    # The token must appear exactly twice in the parsed tree (custom + storage)
    all_keys = [
        parsed["custom"]["embed"]["apiKey"],
        parsed["storage"]["embed"]["apiKey"],
    ]
    assert all_keys.count("FRESH-VALUE") == 1, all_keys
    assert all_keys.count("UNTOUCHED-CUSTOM") == 1, all_keys


# --- Requirement 2: set_provider rerank model must not touch custom.rerank ---

def test_set_provider_rerank_model_does_not_touch_custom_rerank_when_sibling_exists(tmp_path):
    """RED: when both ``custom.rerank`` and ``storage.rerank`` exist,
    ``set_provider('rerank', model=...)`` must mutate
    ``storage.rerank.model`` only.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        custom:
          rerank:
            endpoint: http://custom-rerank.invalid/v1/rerank
            model: UNTOUCHED-CUSTOM-MODEL
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          rerank:
            endpoint: http://rerank.invalid/v1/rerank
            model: OLD-STORAGE-MODEL
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_provider("rerank", model="NEW-STORAGE-MODEL")
    parsed = _read_candidate(p)
    assert parsed["custom"]["rerank"]["model"] == "UNTOUCHED-CUSTOM-MODEL", (
        f"custom.rerank.model must not change; got {parsed['custom']['rerank']['model']!r}"
    )
    assert parsed["storage"]["rerank"]["model"] == "NEW-STORAGE-MODEL", (
        f"storage.rerank.model must equal NEW-STORAGE-MODEL; got {parsed['storage']['rerank']['model']!r}"
    )
    # Endpoint field stays untouched on both sides
    assert parsed["custom"]["rerank"]["endpoint"] == "http://custom-rerank.invalid/v1/rerank"
    assert parsed["storage"]["rerank"]["endpoint"] == "http://rerank.invalid/v1/rerank"


def test_set_provider_rerank_endpoint_does_not_touch_custom_rerank_endpoint(tmp_path):
    """RED: endpoint mutation under same-name sibling must also respect
    parent path. Pairs with the model-only test above.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        custom:
          rerank:
            endpoint: http://custom-rerank.invalid/v1/rerank
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          rerank:
            endpoint: http://rerank.invalid/v1/rerank
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_provider("rerank", endpoint="http://new-rerank.invalid/v2")
    parsed = _read_candidate(p)
    assert parsed["custom"]["rerank"]["endpoint"] == "http://custom-rerank.invalid/v1/rerank", (
        f"custom.rerank.endpoint must not change; got {parsed['custom']['rerank']['endpoint']!r}"
    )
    assert parsed["storage"]["rerank"]["endpoint"] == "http://new-rerank.invalid/v2", (
        f"storage.rerank.endpoint must equal new value; got {parsed['storage']['rerank']['endpoint']!r}"
    )


# --- Requirement 3: set_toggle prefetch.enabled must not touch custom.enabled ---

def test_set_toggle_prefetch_enabled_does_not_touch_custom_enabled_when_sibling_exists(tmp_path):
    """RED: when both ``custom.enabled`` and ``prefetch.enabled`` exist in
    the file, ``set_toggle('prefetch.enabled', value)`` must mutate the
    prefetch slot only.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
        custom:
          enabled: true
        prefetch:
          enabled: true
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_toggle("prefetch.enabled", False)
    parsed = _read_candidate(p)
    assert parsed["custom"]["enabled"] is True, (
        f"custom.enabled must remain True; got {parsed['custom']['enabled']!r}"
    )
    assert parsed["prefetch"]["enabled"] is False, (
        f"prefetch.enabled must be False; got {parsed['prefetch']['enabled']!r}"
    )


def test_set_toggle_prefetch_enabled_with_prefetch_before_custom(tmp_path):
    """RED: when prefetch precedes custom in source order, the same
    invariant must hold (custom.enabled must stay True, prefetch.enabled
    must flip to False).
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
        prefetch:
          enabled: true
        custom:
          enabled: true
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_toggle("prefetch.enabled", False)
    parsed = _read_candidate(p)
    assert parsed["custom"]["enabled"] is True, (
        f"custom.enabled must remain True; got {parsed['custom']['enabled']!r}"
    )
    assert parsed["prefetch"]["enabled"] is False, (
        f"prefetch.enabled must be False; got {parsed['prefetch']['enabled']!r}"
    )


# --- Requirement 4: same-name nested block before AND after intended parent ---

def test_set_keys_with_nested_block_before_and_after_intended_parent(tmp_path):
    """RED: with ``foo.embed`` before ``storage.embed`` AND
    ``foo2.embed`` after ``storage.embed``, mutating
    ``storage.embed.apiKey`` must not touch either same-name sibling.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        foo:
          embed:
            apiKey: UNTOUCHED-FOO
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: OLD-STORAGE
        foo2:
          embed:
            apiKey: UNTOUCHED-FOO2
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_keys(["embedding"], "TARGET-ONLY")
    parsed = _read_candidate(p)
    assert parsed["foo"]["embed"]["apiKey"] == "UNTOUCHED-FOO", (
        f"foo.embed.apiKey must remain UNTOUCHED-FOO; got {parsed['foo']['embed']['apiKey']!r}"
    )
    assert parsed["foo2"]["embed"]["apiKey"] == "UNTOUCHED-FOO2", (
        f"foo2.embed.apiKey must remain UNTOUCHED-FOO2; got {parsed['foo2']['embed']['apiKey']!r}"
    )
    assert parsed["storage"]["embed"]["apiKey"] == "TARGET-ONLY", (
        f"storage.embed.apiKey must equal TARGET-ONLY; got {parsed['storage']['embed']['apiKey']!r}"
    )


# --- Requirement 5: intended child missing but unrelated same-name child present ---

def test_set_keys_when_intended_child_missing_creates_in_correct_parent(tmp_path):
    """RED: with ``storage.embed`` present (no apiKey under it) and an
    unrelated ``custom.embed.apiKey`` already in the file,
    ``set_keys(['embedding'])`` must insert a new ``storage.embed.apiKey``
    and leave ``custom.embed.apiKey`` untouched.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            endpoint: http://embed.invalid/v1/embeddings
            model: m-embed
        custom:
          embed:
            apiKey: UNTOUCHED-CUSTOM
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_keys(["embedding"], "INSERTED-HERE")
    parsed = _read_candidate(p)
    assert parsed["custom"]["embed"]["apiKey"] == "UNTOUCHED-CUSTOM", (
        f"custom.embed.apiKey must not change; got {parsed['custom']['embed']['apiKey']!r}"
    )
    # The new key must live under storage.embed, not anywhere else.
    assert parsed["storage"]["embed"]["apiKey"] == "INSERTED-HERE", (
        f"storage.embed.apiKey must equal INSERTED-HERE; got {parsed['storage']['embed'].get('apiKey')!r}"
    )


# --- Requirement 6: secret exact round-trip (parsed equality, no printing) ---

@pytest.mark.parametrize(
    "secret",
    [
        "with#hash",
        "with:colon",
        'with"doublequote',
        "with'singlequote",
        "with\\backslash",
        "key: value",  # bare scalar with colon — must round-trip parsed-equal
        "  leading-space",  # if service contract allows, must round-trip
        "trailing-space  ",
    ],
)
def test_secret_exact_parsed_round_trip(tmp_path, secret):
    """RED: each secret must round-trip through set_keys → file → PyYAML
    safe_load → parsed-equal to the original input. We assert only on
    the parsed value (parsed equality), never on the raw file text —
    that way the contract is independent of YAML quoting style.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: placeholder
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_keys(["embedding"], secret)
    parsed = _read_candidate(p)
    actual = parsed["storage"]["embed"]["apiKey"]
    # Parsed equality: PyYAML safe_load must give back exactly the original.
    assert actual == secret, (
        f"secret round-trip mismatch for {secret!r}: parsed back as {actual!r}"
    )


def test_secret_yaml_looking_string_round_trips_via_quoting(tmp_path):
    """RED: secrets whose unquoted form would re-parse to a different type
    (e.g. ``"key: value"``) must be written in a quoting style that makes
    PyYAML safe_load return the original string verbatim, not a mapping.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: placeholder
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_keys(["embedding"], "key: value")
    parsed = _read_candidate(p)
    embed = parsed["storage"]["embed"]
    # apiKey must remain a string equal to the original — never a sub-mapping.
    assert isinstance(embed["apiKey"], str), (
        f"apiKey must round-trip as str, got {type(embed['apiKey']).__name__}: {embed['apiKey']!r}"
    )
    assert embed["apiKey"] == "key: value", (
        f"apiKey must equal original secret; got {embed['apiKey']!r}"
    )


# --- Requirement 7: provider endpoint / model exact parsed equality ---

@pytest.mark.parametrize(
    "target,new_endpoint,new_model",
    [
        ("embedding", "http://new-embed.invalid/v1/embeddings", "new-embed"),
        ("rerank", "http://new-rerank.invalid/v1/rerank", "new-rerank"),
        ("llm", "http://new-llm.invalid/v1", "new-llm"),
    ],
)
def test_provider_endpoint_and_model_exact_parsed_equality(
    tmp_path, target, new_endpoint, new_model
):
    """Contract: after set_provider(target, endpoint=..., model=...),
    PyYAML safe_load must yield exactly those values at the intended
    section, with no quoting drift or escape translation.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            endpoint: http://embed.invalid/v1/embeddings
            model: old-embed
          rerank:
            endpoint: http://rerank.invalid/v1/rerank
            model: old-rerank
        llm:
          base_url: http://llm.invalid/v1
          model: old-llm
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    svc.set_provider(target, endpoint=new_endpoint, model=new_model)
    parsed = _read_candidate(p)
    if target == "embedding":
        section = parsed["storage"]["embed"]
    elif target == "rerank":
        section = parsed["storage"]["rerank"]
    else:
        section = parsed["llm"]
    assert section["endpoint" if target != "llm" else "base_url"] == new_endpoint, (
        f"{target} endpoint mismatch: {section!r}"
    )
    assert section["model"] == new_model, f"{target} model mismatch: {section!r}"


# --- Requirement 8: multi-target failure leaves original bytes unchanged ---

def test_multi_target_failure_leaves_original_bytes_unchanged(tmp_path):
    """RED: when one of several simultaneous mutations fails validation,
    the on-disk bytes must remain byte-identical to the original. This
    pins atomicity: a partial-write state must never be observable.
    """
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: ORIG-EMBED
          rerank:
            apiKey: ORIG-RERANK
        llm:
          api_key: ORIG-LLM
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    before = p.read_bytes()

    # Bad secret hygiene should reject before any disk write happens.
    with pytest.raises(ConfigValidationError):
        svc.set_keys(["embedding", "rerank", "llm"], "bad\x16key")
    assert p.read_bytes() == before, (
        "atomic_update must leave file unchanged when secret hygiene rejects"
    )

    # Unknown target should also reject without partial writes.
    with pytest.raises(ConfigValidationError):
        svc.set_keys(["embedding", "does-not-exist"], "another")
    assert p.read_bytes() == before, (
        "atomic_update must leave file unchanged when a target is unknown"
    )

    # Candidate that breaks YAML parse must also leave the file unchanged.
    with pytest.raises(ConfigValidationError):
        svc.atomic_update(lambda _t: "storage: [")
    assert p.read_bytes() == before, (
        "atomic_update must leave file unchanged when candidate fails to parse"
    )


# --- Requirement 9: CRLF / comments / unknown fields preserved ---

def test_crlf_comments_and_unknown_fields_preserved_through_mutation(tmp_path):
    """RED: when mutating a CRLF file that contains comments and unknown
    fields, the new file must keep CRLF line endings, the comment text,
    and the unknown field name+value byte-for-byte intact.
    """
    text = (
        "basePath: C:/config-lab\r\n"
        "# header comment that must survive\r\n"
        "storage:\r\n"
        "  pg:\r\n"
        "    host: 127.0.0.1\r\n"
        "    port: 55453\r\n"
        "    database: g5a\r\n"
        "    user: g5a\r\n"
        "  embed:\r\n"
        "    apiKey: OLD-VAL\r\n"
        "    # inline preserve comment\r\n"
        "    unknown_embed_field: keep-me-please\r\n"
    )
    p = tmp_path / "config.yaml"
    p.write_bytes(text.encode("utf-8"))
    svc = ConfigControlService(path=p)
    svc.set_keys(["embedding"], "FRESH-VAL")
    new_bytes = p.read_bytes()
    new_text = new_bytes.decode("utf-8")

    # CRLF preserved: every newline in the new content must be CRLF.
    crlf_count = new_text.count("\r\n")
    lone_lf_count = new_text.count("\n") - crlf_count
    assert lone_lf_count == 0, (
        f"CRLF was silently converted to LF: {lone_lf_count} lone-LF remain"
    )
    assert crlf_count >= 5, "expected at least 5 CRLF sequences in the output"

    # Comments preserved.
    assert "header comment that must survive" in new_text
    assert "inline preserve comment" in new_text

    # Unknown field preserved verbatim.
    assert "unknown_embed_field: keep-me-please" in new_text

    # The actual mutation took effect (parsed equality).
    parsed = yaml.safe_load(new_text)
    assert parsed["storage"]["embed"]["apiKey"] == "FRESH-VAL"
    assert parsed["storage"]["embed"]["unknown_embed_field"] == "keep-me-please"


# --- Requirement 10: failure diagnostics / stdout / stderr do not contain secret ---

def test_failure_diagnostics_do_not_echo_secret(tmp_path):
    """RED: when a candidate mutation fails for any reason (secret hygiene,
    YAML parse, structural validation), the resulting ConfigValidationError
    message must NOT contain the secret value. We probe three failure paths.
    """
    secret = _UNIQUE_LEAK_SENTINEL  # never printed, never interpolated into strings
    text = textwrap.dedent(
        """\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            apiKey: placeholder
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)

    # Path 1: secret hygiene (control char) — message must not echo secret.
    bad_secret = secret + "\x16suffix"
    with pytest.raises(ConfigValidationError) as exc1:
        svc.set_keys(["embedding"], bad_secret)
    msg1 = str(exc1.value)
    assert secret not in msg1, (
        f"ConfigValidationError leaked the secret in the control-char path: {msg1!r}"
    )

    # Path 2: candidate YAML parse failure — message must not echo secret.
    # Inject secret into a position that makes the resulting text unparseable.
    def _candidate_with_secret_leak(_t: str) -> str:
        # Malformed YAML: secret becomes an orphan key, breaking the mapping.
        return f"storage:\n  pg: {secret}: oops\n"

    with pytest.raises(ConfigValidationError) as exc2:
        svc.atomic_update(_candidate_with_secret_leak)
    msg2 = str(exc2.value)
    assert secret not in msg2, (
        f"ConfigValidationError leaked the secret in the parse-error path: {msg2!r}"
    )

    # Path 3: candidate breaks structural validation (bad port type) — must not leak.
    def _candidate_bad_type(_t: str) -> str:
        return _t.replace("55453", secret).replace("apiKey: placeholder", f"apiKey: {secret}")

    with pytest.raises(ConfigValidationError) as exc3:
        svc.atomic_update(_candidate_bad_type)
    msg3 = str(exc3.value)
    assert secret not in msg3, (
        f"ConfigValidationError leaked the secret in the structural-validation path: {msg3!r}"
    )


def test_show_redacts_secret_and_safe_endpoint_when_siblings_exist(tmp_path):
    """RED: even when an unrelated same-name sibling section exists,
    ``show(redact=False)`` must not echo the apiKey of any provider. This
    pins the redaction invariant independent of the mutation pipeline.
    """
    secret_embed = "redact-me-embed-X9Q"
    secret_custom = "redact-me-custom-Y7P"
    text = textwrap.dedent(
        f"""\
        basePath: C:/config-lab
        storage:
          pg:
            host: 127.0.0.1
            port: 55453
            database: g5a
            user: g5a
          embed:
            endpoint: http://u:p@embed.invalid/v1/embeddings?token=should-not-leak
            model: m-embed
            apiKey: {secret_embed}
        custom:
          embed:
            apiKey: {secret_custom}
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    svc = ConfigControlService(path=p)
    human = svc.show(redact=False)
    payload = json.loads(svc.show(redact=False, as_json=True))
    combined = human + json.dumps(payload)
    assert secret_embed not in combined, (
        "show(redact=False) leaked storage.embed apiKey"
    )
    assert secret_custom not in combined, (
        "show(redact=False) leaked custom.embed apiKey"
    )
    assert "should-not-leak" not in combined, (
        "show(redact=False) leaked endpoint query token"
    )
    assert "***@" in human, "userinfo should be masked"


def test_semantic_round_trip_mismatch_leaves_source_unchanged(tmp_path, monkeypatch):
    path = _write_config(tmp_path)
    original = path.read_bytes()
    real_mutate = _cc._mutate_scalar

    def corrupt_candidate(text, parent, field, value):
        candidate = real_mutate(text, parent, field, value)
        if field in {"api_key", "apiKey"}:
            candidate = candidate.replace(
                "api_key: requested-value", "api_key: corrupted-value"
            )
            candidate = candidate.replace(
                "apiKey: requested-value", "apiKey: corrupted-value"
            )
        return candidate

    monkeypatch.setattr(_cc, "_mutate_scalar", corrupt_candidate)
    with pytest.raises(ConfigValidationError) as exc:
        ConfigControlService(path=path).set_keys(
            ["embedding"], "requested-value"
        )
    assert any(
        issue.code == "EXPECTED_VALUE_MISMATCH"
        for issue in exc.value.issues
    )
    assert path.read_bytes() == original
    assert "requested-value" not in str(exc.value)


# ---------------------------------------------------------------------------
# Additions: parsed-round-trip coverage + multi-target set_keys + service
# boundary control-char rejection. Tests only assert on parsed equality; no
# production diagnostics print secret values.
# ---------------------------------------------------------------------------


_EXTRA_SECRET_ROUND_TRIP_CASES = [
    "plain-key",
    "key-with-#",
    "abc #tail",
    "key-with-colon",
    "abc: tail",
    'has "double" quote',
    "has \\backslash",
    "true",
    "false",
    "null",
    "12345",
    "*alias",
    "&anchor",
    "{x}",
    "[x]",
]


@pytest.mark.parametrize("secret_value", _EXTRA_SECRET_ROUND_TRIP_CASES)
def test_secret_exact_parsed_round_trip_extra(tmp_path, secret_value):
    """Every named secret shape must parse back to the exact input string."""
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    service.set_keys(["embedding"], secret_value)
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["storage"]["embed"]["api_key"] == secret_value
    assert parsed["storage"]["rerank"]["apiKey"] == FAKE_RERANK_KEY
    assert parsed["llm"]["api_key"] == FAKE_LLM_KEY
    assert parsed["storage"]["embed"]["unknown_embed_field"] == "keep-me"
    assert parsed["custom_forward_compatible"]["keep"] is True


def test_set_keys_multi_target_exact_round_trip(tmp_path):
    """One multi-target call must write the same exact secret to all targets."""
    path = _write_config(tmp_path)
    service = ConfigControlService(path=path)
    before = path.read_bytes()
    complex_secret = "abc #tail"
    service.set_keys(["embedding", "rerank", "llm"], complex_secret)
    after = path.read_bytes()
    assert after != before
    parsed = yaml.safe_load(after)
    assert parsed["storage"]["embed"]["api_key"] == complex_secret
    assert parsed["storage"]["rerank"]["apiKey"] == complex_secret
    assert parsed["llm"]["api_key"] == complex_secret
    assert parsed["storage"]["pg"]["password"] == FAKE_PG_PASSWORD
    assert parsed["storage"]["embed"]["unknown_embed_field"] == "keep-me"
    assert parsed["custom_forward_compatible"]["keep"] is True


_FORBIDDEN_CONTROL_CHARS = [
    pytest.param("\t", id="TAB"),
    pytest.param("\n", id="LF"),
    pytest.param("\r", id="CR"),
    pytest.param("\x7f", id="DEL"),
    pytest.param("\x16", id="U+0016-SYN"),
]


@pytest.mark.parametrize("bad_char", _FORBIDDEN_CONTROL_CHARS)
def test_service_boundary_rejects_forbidden_control_chars(bad_char, tmp_path):
    """Service rejects controls, preserves bytes, and does not echo secrets."""
    path = _write_config(tmp_path)
    before_bytes = path.read_bytes()
    service = ConfigControlService(path=path)
    bad_secret = f"before{bad_char}after"
    with pytest.raises(ConfigValidationError) as exc_info:
        service.set_keys(["embedding"], bad_secret)
    assert path.read_bytes() == before_bytes
    rendered = str(exc_info.value)
    assert bad_secret not in rendered
    assert any(issue.code == "SECRET_CONTROL_CHAR" for issue in exc_info.value.issues)
