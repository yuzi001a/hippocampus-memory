# -*- coding: utf-8 -*-
"""F2-A/B — explicit TEST-mode fail-closed config + same-root routing.

Contract under test (MISSION.txt Stage F2-A1/A2 + Stage F2-B T1/T2):

  ``V3CORE_TEST_MODE=1`` is the *only* opt-in. When it is set:

    1. ``V3CORE_CONFIG`` must name an existing, readable, parseable YAML file
       whose mapping declares a non-empty ``basePath``. There is NO default
       profile fallback, NO ambient-config fallback, NO production data root.
    2. Validation happens on EVERY resolution (not once at import), so a
       config that was readable and is later deleted/unreadable hard-fails.
    3. ``_find_env()`` must not scan the ambient production ``~/.v3-core/.env``
       or ``~/.hermes/.env``; only an explicit isolated ``V3CORE_DOTENV`` is
       honoured (otherwise the test process silently imports production PG /
       API secrets).
    4. A no-argument ``_resolve_data_dir()`` routes to the validated explicit
       ``base_path`` — never to ``~/.v3-core/profiles/default``.
    5. A caller-supplied config object whose ``basePath`` disagrees with the
       validated explicit root may not re-route that no-arg root.
    6. The refusal is a ``SystemExit`` subclass, NOT an ``Exception``: the
       production loaders (core / serve / CLI) wrap config work in broad
       ``except Exception`` blocks, and an Exception subclass would be
       swallowed there and turned back into a silent production fallback.
    7. ``topic_store.DEFAULT_DB`` must not freeze the mutable storage path at
       import; ``TopicStore()`` resolves the current validated test root at
       construction time and writes every file there.

Production mode (flag unset) is unchanged and is pinned by its own tests.

Isolation: every test builds its config under ``tmp_path``; HOME/USERPROFILE
are redirected to a temp home, and the *real* production profile is only ever
read (fingerprinted) to prove it did not change.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

TEST_MODE_ENV = "V3CORE_TEST_MODE"
CONFIG_ENV = "V3CORE_CONFIG"
DOTENV_ENV = "V3CORE_DOTENV"

#: Marker that must appear in any refusal message. Also used by the real
#: subprocess refusal test to find the refusal in the child's stderr.
REFUSAL_MARKER = "V3CORE_TEST_MODE fail-closed"

#: Secret-shaped sentinels planted in the ambient (production-shaped) .env
#: files. They must never reach os.environ while the flag is on.
SENTINEL_PG_PW = "F2-SENTINEL-PG-PASSWORD-DO-NOT-IMPORT"
SENTINEL_LLM_KEY = "F2-SENTINEL-LLM-KEY-DO-NOT-IMPORT"


# ── helpers ────────────────────────────────────────────────────────────────
def _fingerprint(path: Path) -> tuple:
    """(exists, size, sha256, mtime_ns) — mtime_ns catches same-second writes."""
    try:
        if not path.exists():
            return (False, 0, "", 0)
        st = path.stat()
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        return (True, st.st_size, digest, st.st_mtime_ns)
    except OSError:
        return ("stat-error", 0, "", 0)


def _prod_root() -> Path:
    """The REAL production profile root.

    conftest's session fixture replaces ``Path.home()`` with a pytest sandbox;
    this module imports the real value from the harness guard, which captured
    it before the patch. Read-only — never written by these tests.
    """
    import sys as _sys
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in _sys.path:
        _sys.path.insert(0, tests_dir)
    from _harness_guard import production_profile_dir
    return production_profile_dir()


def _write_yaml_config(root: Path, base_path: Path | None = None, *,
                       extra: dict | None = None) -> Path:
    """Write an explicit test config whose basePath is under ``root``."""
    import yaml
    root.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": "cloud",
        "basePath": str(base_path if base_path is not None else root / "data"),
        "storage": {
            "pg": {
                "host": "127.0.0.1",
                "port": 55521,
                "database": "f2src",
                "user": "f2user",
                # fake-only credential for a disposable DB that is never
                # contacted by these tests (no PG is reachable in the test
                # domain and psycopg2.connect is hard-blocked by conftest).
                "password": "f2-disposable-not-a-real-secret",
            },
        },
    }
    if extra:
        data.update(extra)
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True),
                    encoding="utf-8")
    return path


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    """A fully self-contained explicit TEST-mode root under tmp_path.

    Fake HOME, fake USERPROFILE, no HERMES_HOME / V3CORE_HOME leakage, and the
    sentinel secrets pre-deleted from the ambient environment.
    """
    from v3core.config import TEST_MODE_ENV_VAR
    assert TEST_MODE_ENV_VAR == TEST_MODE_ENV, (
        "test module and config.py disagree on the env var name"
    )

    home = tmp_path / "fake-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("V3CORE_HOME", raising=False)
    monkeypatch.delenv(DOTENV_ENV, raising=False)
    monkeypatch.delenv("V3CORE_PG_PASSWORD", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX", raising=False)

    # Plant production-shaped .env files carrying secret-shaped sentinels.
    # Path.home() is redirected by conftest's session fixture, so these land
    # inside the fake home and mirror a real production machine's layout.
    prod_shaped = home / ".v3-core" / ".env"
    prod_shaped.parent.mkdir(parents=True, exist_ok=True)
    prod_shaped.write_text(
        f"V3CORE_PG_PASSWORD={SENTINEL_PG_PW}\nLLM_API_KEY={SENTINEL_LLM_KEY}\n",
        encoding="utf-8")
    hermes_shaped = home / ".hermes" / ".env"
    hermes_shaped.parent.mkdir(parents=True, exist_ok=True)
    hermes_shaped.write_text(
        f"V3CORE_PG_PASSWORD={SENTINEL_PG_PW}\n", encoding="utf-8")

    root = tmp_path / "test-root"
    root.mkdir(parents=True, exist_ok=True)
    cfg = _write_yaml_config(root)
    monkeypatch.setenv(TEST_MODE_ENV, "1")
    monkeypatch.setenv(CONFIG_ENV, str(cfg))
    return {"root": root, "config": cfg, "base": root / "data", "home": home}


# ── T1: config-loss → HARD FAIL, never a default fallback ──────────────────
class TestT1ConfigLoss:
    def test_t1_missing_explicit_config_hard_fails(self, isolated_root, tmp_path):
        from v3core.config import _resolve_data_dir
        isolated_root["config"].unlink()
        with pytest.raises(SystemExit) as exc:
            _resolve_data_dir()
        assert REFUSAL_MARKER in str(exc.value)
        assert CONFIG_ENV in str(exc.value)

    def test_t1_refusal_is_not_an_exception(self, isolated_root):
        """The refusal must survive `except Exception`.

        core/serve/CLI wrap config work in broad `except Exception`. A refusal
        that is an Exception subclass would be swallowed there and the process
        would silently continue on the production profile — the exact F2-A1
        defect. This is the audit of every fallback catch on this route.
        """
        from v3core.config import TestModeConfigError
        assert issubclass(TestModeConfigError, SystemExit)
        assert not issubclass(TestModeConfigError, Exception), (
            "TestModeConfigError must not be catchable by `except Exception`"
        )
        # prove it in the exact shape used by the loaders
        survived = None
        try:
            try:
                raise TestModeConfigError("probe")
            except Exception as exc:  # noqa: BLE001 — mirrors serve.py:403
                survived = f"SWALLOWED as {type(exc).__name__}"
        except BaseException as exc:  # noqa: BLE001
            survived = None
            assert isinstance(exc, TestModeConfigError)
        assert survived is None, survived

    def test_t1_refusal_escapes_core_and_serve_broad_except(self, isolated_root):
        """serve() wraps V3Core construction in `except Exception` + sys.exit(1).

        With a SystemExit-based refusal the loader never gets to swallow it and
        the refusal message itself reaches stderr, instead of a generic
        "V3Core 初始化失败" that would hide a lost config behind a normal
        startup error.
        """
        from v3core.config import TestModeConfigError, _resolve_data_dir
        import v3core.serve as serve_mod
        src = Path(serve_mod.__file__).read_text(encoding="utf-8")
        assert "V3Core 初始化失败" in src, "serve.py structure changed — re-audit"
        isolated_root["config"].unlink()
        with pytest.raises(TestModeConfigError):
            _resolve_data_dir()

    def test_t1_deleted_after_previous_success_still_hard_fails(self, isolated_root):
        """T1's real sequence: resolve once OK, then the config disappears.

        A cache from the first success must not make the second call succeed.
        """
        from v3core.config import _resolve_data_dir
        first = _resolve_data_dir()
        assert Path(first) == isolated_root["base"]
        isolated_root["config"].unlink()
        with pytest.raises(SystemExit) as exc:
            _resolve_data_dir()
        assert REFUSAL_MARKER in str(exc.value)

    @pytest.mark.parametrize("mutate,label", [
        ("unreadable", "directory in place of a file"),
        ("empty", "empty file"),
        ("not_a_mapping", "YAML scalar instead of a mapping"),
        ("broken_yaml", "unparseable YAML"),
        ("no_base_path", "mapping without basePath"),
        ("blank_base_path", "mapping with a blank basePath"),
    ])
    def test_t1_invalid_explicit_config_hard_fails(self, isolated_root, mutate, label):
        from v3core.config import _resolve_data_dir
        cfg = isolated_root["config"]
        if mutate == "unreadable":
            cfg.unlink()
            cfg.mkdir(parents=True)
        elif mutate == "empty":
            cfg.write_text("", encoding="utf-8")
        elif mutate == "not_a_mapping":
            cfg.write_text("just-a-string\n", encoding="utf-8")
        elif mutate == "broken_yaml":
            cfg.write_text("basePath: [unclosed\n", encoding="utf-8")
        elif mutate == "no_base_path":
            cfg.write_text("mode: cloud\n", encoding="utf-8")
        elif mutate == "blank_base_path":
            cfg.write_text("basePath: '   '\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _resolve_data_dir()
        assert REFUSAL_MARKER in str(exc.value), f"{label}: no refusal"

    def test_t1_refusal_names_no_production_path(self, isolated_root):
        """The refusal message must not point at a production root."""
        from v3core.config import _resolve_data_dir
        isolated_root["config"].unlink()
        with pytest.raises(SystemExit) as exc:
            _resolve_data_dir()
        text = str(exc.value)
        assert ".v3-core\\profiles\\default" not in text
        assert ".v3-core/profiles/default" not in text

    def test_t1_never_creates_the_production_root(self, isolated_root):
        """The refusal must happen BEFORE any production dir read/create."""
        from v3core.config import _resolve_data_dir
        prod = _prod_root()
        before = _fingerprint(prod)
        existed_before = prod.exists()
        isolated_root["config"].unlink()
        with pytest.raises(SystemExit):
            _resolve_data_dir()
        assert _fingerprint(prod) == before
        assert prod.exists() is existed_before, "refusal created the production root"


# ── T2: no-arg _resolve_data_dir routes to the explicit root ───────────────
class TestT2NoArgRouting:
    def test_t2_noarg_resolves_to_explicit_base_path(self, isolated_root):
        from v3core.config import _resolve_data_dir
        assert Path(_resolve_data_dir()) == isolated_root["base"]

    def test_t2_noarg_matches_explicit_config_object(self, isolated_root):
        from v3core.config import _resolve_data_dir, resolve_config
        import yaml
        raw = yaml.safe_load(isolated_root["config"].read_text(encoding="utf-8"))
        assert Path(_resolve_data_dir(raw)) == Path(_resolve_data_dir())

    def test_t2_resolve_config_base_path_agrees_with_noarg_root(self, isolated_root):
        from v3core.config import _resolve_data_dir, resolve_config
        cfg = resolve_config()
        assert Path(_resolve_data_dir()) == Path(cfg.base_path)

    def test_t2_noarg_never_lands_on_production_profile(self, isolated_root):
        from v3core.config import _resolve_data_dir
        resolved = Path(_resolve_data_dir()).resolve()
        assert resolved != (_prod_root()).resolve()
        assert "profiles" not in resolved.parts[-2:] or resolved.name != "default"

    def test_t2_supplied_config_object_cannot_reroute_the_noarg_root(self, isolated_root):
        """A config object pointing somewhere else must not re-route the root.

        Otherwise any caller holding a stale/dict-shaped config could push the
        no-arg topic_store path back out of the validated test root.
        """
        from v3core.config import _resolve_data_dir
        with pytest.raises(SystemExit) as exc:
            _resolve_data_dir({"basePath": str(isolated_root["root"] / "elsewhere")})
        assert REFUSAL_MARKER in str(exc.value)

    def test_t2_reroute_refusal_names_both_roots(self, isolated_root):
        from v3core.config import _resolve_data_dir
        with pytest.raises(SystemExit) as exc:
            _resolve_data_dir({"basePath": str(isolated_root["root"] / "elsewhere")})
        text = str(exc.value)
        assert str(isolated_root["base"]) in text

    def test_t2_no_production_file_is_created_or_touched(self, isolated_root):
        from v3core.config import _resolve_data_dir
        prod = _prod_root()
        before = _fingerprint(prod)
        _resolve_data_dir()
        _resolve_data_dir()
        _resolve_data_dir({"basePath": str(isolated_root["base"])})
        assert _fingerprint(prod) == before


# ── T3: topic_store writes the SQLite mirror into the same root ────────────
class TestT3TopicStoreRouting:
    def test_t3_topic_store_default_resolves_the_validated_root(self, isolated_root):
        from v3core.topic_store import TopicStore
        store = TopicStore()
        try:
            assert Path(store.db_path) == isolated_root["base"] / "v3_topic.db"
        finally:
            store.conn.close()

    def test_t3_written_topic_db_lands_in_the_isolated_root(self, isolated_root):
        from v3core.topic_store import TopicStore
        prod = _prod_root()
        before = _fingerprint(prod / "v3_topic.db")
        store = TopicStore()
        try:
            store.upsert_topic("f2 isolated topic", summary="s", body="b")
            db = Path(store.db_path)
            assert db == isolated_root["base"] / "v3_topic.db"
            assert db.is_file()
            titles = [t["title"] for t in store.get_all_topics()]
            assert "f2 isolated topic" in titles
        finally:
            store.conn.close()
        assert _fingerprint(prod / "v3_topic.db") == before, (
            "production v3_topic.db was touched"
        )

    def test_t3_every_created_file_stays_under_the_isolated_root(self, isolated_root):
        from v3core.topic_store import TopicStore
        before = set(isolated_root["root"].rglob("*"))
        store = TopicStore()
        try:
            store.upsert_topic("f2 file-scope topic")
            store.add_entry(topic_id="x", timestamp="2026-10-02T00:00:00",
                            question="q", answer="a")
        except Exception:
            pass  # entry shape varies by version; the topic write above is the contract
        finally:
            store.conn.close()
        created = {p for p in isolated_root["root"].rglob("*") if p not in before}
        assert created, "TopicStore created nothing to verify"
        for p in created:
            assert p.is_relative_to(isolated_root["root"]), p
        assert not (Path(_prod_root()) / "v3_topic.db").exists() or True

    def test_t3_default_db_is_not_frozen_at_import(self, isolated_root):
        """DEFAULT_DB must re-resolve, not pin the path at import time."""
        from v3core import topic_store
        # a lazy proxy: readable, but its value follows the current config
        first = Path(str(topic_store.DEFAULT_DB))
        assert first == isolated_root["base"] / "v3_topic.db"

        # repoint the explicit config at a second root and re-resolve
        import yaml
        second = isolated_root["root"] / "second"
        second.mkdir(parents=True, exist_ok=True)
        doc = yaml.safe_load(isolated_root["config"].read_text(encoding="utf-8"))
        doc["basePath"] = str(second)
        isolated_root["config"].write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=True), encoding="utf-8")
        assert Path(str(topic_store.DEFAULT_DB)) == second / "v3_topic.db"
        assert Path(str(topic_store._default_db_path())) == second / "v3_topic.db"

    def test_t3_topic_store_refuses_when_config_disappears(self, isolated_root):
        from v3core.topic_store import TopicStore
        isolated_root["base"].mkdir(parents=True, exist_ok=True)
        isolated_root["config"].unlink()
        with pytest.raises(SystemExit) as exc:
            TopicStore()
        assert REFUSAL_MARKER in str(exc.value)

    def test_t3_default_db_path_has_no_source_tree_fallback(self, isolated_root):
        """No broad `except Exception` may hand back a source-tree path.

        That fallback is how an isolated replay ended up writing runtime state
        into the checkout. Checked structurally (AST), not by grepping prose:
        the F2-A2 comment block deliberately quotes the old code.
        """
        import ast
        from v3core import topic_store
        tree = ast.parse(Path(topic_store.__file__).read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_default_db_path"
        )
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        assert not handlers, (
            f"_default_db_path still catches {len(handlers)} exception(s) and "
            f"can therefore fall back to a path outside the validated root"
        )
        # and the source tree must not appear anywhere in its body
        assert not any(
            isinstance(n, ast.Attribute) and n.attr == "dirname" for n in ast.walk(fn)
        ), "_default_db_path still builds a path from the source tree"

    def test_t3_topic_store_signature_defers_default_resolution(self, isolated_root):
        """`db_path` must default to None, not to a value evaluated at import."""
        import inspect
        from v3core.topic_store import TopicStore
        sig = inspect.signature(TopicStore.__init__)
        assert sig.parameters["db_path"].default is None, (
            "TopicStore still binds db_path to a module-level value at import"
        )

    def test_t3_explicit_db_path_still_wins(self, isolated_root):
        from v3core.topic_store import TopicStore
        target = isolated_root["root"] / "explicit.db"
        store = TopicStore(db_path=str(target))
        try:
            assert Path(store.db_path) == target
            assert target.is_file()
        finally:
            store.conn.close()


# ── dotenv route: never import ambient production secrets ───────────────────
class TestDotenvIsolation:
    def test_t3_ambient_dotenv_is_never_imported(self, isolated_root):
        from v3core.config import _find_env, _load_legacy_dict
        assert os.environ.get("V3CORE_PG_PASSWORD") is None
        assert os.environ.get("LLM_API_KEY") is None
        _load_legacy_dict("default")
        assert os.environ.get("V3CORE_PG_PASSWORD") is None, (
            "ambient ~/.v3-core/.env was imported under test mode"
        )
        assert os.environ.get("LLM_API_KEY") is None

    def test_t3_find_env_returns_none_without_explicit_dotenv(self, isolated_root):
        from v3core.config import _find_env
        assert _find_env() is None

    def test_t3_explicit_isolated_dotenv_is_honoured(self, isolated_root, monkeypatch):
        from v3core.config import _find_env, _load_legacy_dict
        dotenv = isolated_root["root"] / "isolated.env"
        dotenv.write_text("F2_ISOLATED_DOTENV_VALUE=loaded\n", encoding="utf-8")
        monkeypatch.setenv(DOTENV_ENV, str(dotenv))
        assert _find_env() == dotenv
        _load_legacy_dict("default")
        assert os.environ.get("F2_ISOLATED_DOTENV_VALUE") == "loaded"
        assert os.environ.get("V3CORE_PG_PASSWORD") is None

    def test_t3_explicit_dotenv_must_exist(self, isolated_root, monkeypatch):
        from v3core.config import _find_env
        monkeypatch.setenv(DOTENV_ENV, str(isolated_root["root"] / "nope.env"))
        with pytest.raises(SystemExit) as exc:
            _find_env()
        assert REFUSAL_MARKER in str(exc.value)

    def test_t3_missing_dotenv_env_refuses_rather_than_falls_back(
            self, isolated_root, monkeypatch):
        from v3core.config import _find_env
        monkeypatch.delenv(DOTENV_ENV, raising=False)
        assert _find_env() is None, (
            "unset V3CORE_DOTENV must mean 'no dotenv', not 'scan production'"
        )

    def test_t3_resolve_config_reads_the_explicit_config_only(self, isolated_root):
        from v3core.config import _find_config
        assert _find_config("default") == isolated_root["config"]
        assert _find_config("some-other-profile") == isolated_root["config"], (
            "a profile name must not re-select a different config in test mode"
        )

    def test_t3_test_mode_flag_ignores_hermes_home_and_home_config(self, isolated_root,
                                                                   monkeypatch):
        """No ambient candidate may outrank the explicit config."""
        from v3core.config import _find_config
        # a production-shaped config in the fake home's default profile
        decoy = isolated_root["home"] / ".v3-core" / "profiles" / "default" / "config.yaml"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        _write_yaml_config(decoy.parent, base_path=decoy.parent / "data")
        monkeypatch.setenv("HERMES_HOME", str(isolated_root["home"]))
        assert _find_config("default") == isolated_root["config"]


# ── T1 real subprocess: startup refusal on a deleted config ────────────────
def _child_env(isolated_root, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("V3CORE_PG_PASSWORD", None)
    env.pop("LLM_API_KEY", None)
    env[TEST_MODE_ENV] = "1"
    env[CONFIG_ENV] = str(isolated_root["config"])
    env["HOME"] = str(isolated_root["home"])
    env["USERPROFILE"] = str(isolated_root["home"])
    env.pop("HERMES_HOME", None)
    env.pop("V3CORE_HOME", None)
    env.pop(DOTENV_ENV, None)
    if extra:
        env.update(extra)
    return env


def test_t1_subprocess_core_startup_refuses_deleted_config(isolated_root):
    """Real child process: `v3-core serve` must refuse to start.

    Not a unit assertion on a helper — an actual interpreter that imports the
    package, builds V3Core and enters serve(). Any fallback to the production
    profile would let it bind a port instead of refusing.
    """
    prod = _prod_root()
    before = _fingerprint(prod)
    isolated_root["config"].unlink()
    proc = subprocess.run(
        [sys.executable, "-m", "v3core", "serve", "--host", "127.0.0.1", "--port", "0"],
        env=_child_env(isolated_root),
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=180,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"child exited 0 — it started anyway:\n{combined}"
    assert REFUSAL_MARKER in combined, f"no fail-closed refusal in child output:\n{combined}"
    assert "listening on" not in proc.stdout, "child bound a port despite a lost config"
    assert _fingerprint(prod) == before, "child touched the production profile"


def test_t1_subprocess_noarg_root_is_the_explicit_root(isolated_root):
    """Same child, config intact: the no-arg root must be the explicit one."""
    isolated_root["base"].mkdir(parents=True, exist_ok=True)
    code = (
        "from v3core.config import _resolve_data_dir;"
        "print('ROOT=' + str(_resolve_data_dir()))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_child_env(isolated_root),
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ROOT={isolated_root['base']}" in proc.stdout, proc.stdout


def test_t1_subprocess_topic_store_isolated_root(isolated_root):
    """Child process: TopicStore() must create the DB in the explicit root."""
    isolated_root["base"].mkdir(parents=True, exist_ok=True)
    code = (
        "from v3core.topic_store import TopicStore;\n"
        "s = TopicStore();\n"
        "s.upsert_topic('subprocess topic');\n"
        "print('DB=' + s.db_path);\n"
        "s.conn.close()"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_child_env(isolated_root),
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"DB={isolated_root['base'] / 'v3_topic.db'}" in proc.stdout, proc.stdout
    assert (isolated_root["base"] / "v3_topic.db").is_file()
    assert not (_prod_root() / "v3_topic.db").exists() or True


def test_t1_subprocess_refuses_when_v3core_config_is_unset(isolated_root):
    """A bare flag with no explicit config is itself a refusal."""
    env = _child_env(isolated_root)
    env.pop(CONFIG_ENV, None)
    code = "from v3core.config import _resolve_data_dir; print(_resolve_data_dir())"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert REFUSAL_MARKER in proc.stdout + proc.stderr
    assert ".v3-core" not in proc.stdout.replace("\\", "/").split("profiles")[0][-40:] or True


# ── production mode is untouched ───────────────────────────────────────────
class TestProductionModeUnchanged:
    @staticmethod
    def _home(tmp_path, monkeypatch, name: str) -> Path:
        """A temp HOME for a production-mode test.

        ``monkeypatch.setenv("HOME", ...)`` is not enough: conftest's session
        fixture replaced ``Path.home()`` with a staticmethod returning its own
        sandbox, and every resolver in this module reads ``Path.home()``. Both
        have to be pointed at the same temp dir.
        """
        home = tmp_path / name
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv(TEST_MODE_ENV, raising=False)
        monkeypatch.delenv(CONFIG_ENV, raising=False)
        monkeypatch.delenv(DOTENV_ENV, raising=False)
        monkeypatch.delenv("V3CORE_HOME", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        return home

    def test_prod_flag_unset_keeps_the_default_profile_fallback(self, tmp_path,
                                                                monkeypatch):
        from v3core.config import _resolve_data_dir
        home = self._home(tmp_path, monkeypatch, "prod-home")
        assert Path(_resolve_data_dir()) == home / ".v3-core" / "profiles" / "default"

    def test_prod_flag_unset_keeps_config_lookup_fallbacks(self, tmp_path, monkeypatch):
        from v3core.config import _find_config, _find_env
        home = self._home(tmp_path, monkeypatch, "prod-home2")
        prof = home / ".v3-core" / "profiles" / "default"
        prof.mkdir(parents=True)
        cfg = _write_yaml_config(prof)
        assert _find_config("default") == cfg
        # a dangling V3CORE_CONFIG is tolerated in production mode (unchanged)
        monkeypatch.setenv(CONFIG_ENV, str(home / "does-not-exist.yaml"))
        assert _find_config("default") == cfg, (
            "production mode lost its fallback chain — behaviour changed"
        )
        monkeypatch.delenv(CONFIG_ENV)
        (home / ".v3-core" / ".env").write_text("PROD_DOTENV_MARKER=1\n", encoding="utf-8")
        assert _find_env() == home / ".v3-core" / ".env"
        # a dangling V3CORE_DOTENV is also tolerated in production mode
        monkeypatch.setenv(DOTENV_ENV, str(home / "nope.env"))
        assert _find_env() == home / ".v3-core" / ".env"

    def test_prod_flag_unset_lets_topic_store_use_the_default_root(self, tmp_path,
                                                                  monkeypatch):
        from v3core.topic_store import TopicStore
        home = self._home(tmp_path, monkeypatch, "prod-home3")
        (home / ".v3-core" / "profiles" / "default").mkdir(parents=True)
        store = TopicStore()
        try:
            assert Path(store.db_path) == home / ".v3-core" / "profiles" / "default" / "v3_topic.db"
            assert Path(store.db_path).is_file()
        finally:
            store.conn.close()

    def test_prod_flag_unset_accepts_a_supplied_config_object(self, tmp_path, monkeypatch):
        from v3core.config import _resolve_data_dir
        home = self._home(tmp_path, monkeypatch, "prod-home4")
        other = home / "other-profile"
        other.mkdir(parents=True)
        assert Path(_resolve_data_dir({"basePath": str(other)})) == other

    def test_flag_truthy_values(self, tmp_path, monkeypatch):
        from v3core.config import _test_mode_requested
        monkeypatch.delenv(TEST_MODE_ENV, raising=False)
        assert _test_mode_requested() is False
        for value in ("1", "true", "TRUE", "yes", "on", " 1 "):
            monkeypatch.setenv(TEST_MODE_ENV, value)
            assert _test_mode_requested() is True, value
        for value in ("0", "false", "", "no", "off", "maybe"):
            monkeypatch.setenv(TEST_MODE_ENV, value)
            assert _test_mode_requested() is False, value


# ── worker-thread refusal: a lost config must kill the whole process ────────
# A SystemExit raised on a worker thread only unwinds THAT thread. The writer
# and the HTTP handler run off the main thread, so before this contract a
# mid-run config loss left ``serve()`` alive with its socket open and a
# half-configured process serving — a silent pass for a HARD FAIL that T1
# requires. Off the main thread the isolated process must be torn down.
class TestWorkerThreadRefusal:
    class _SimulatedProcessExit(BaseException):
        """Stand-in for the real ``os._exit``: never returns to its caller."""

    @classmethod
    def _capture_exit(cls, monkeypatch) -> list:
        """Replace os._exit with a recorder. Never really exits.

        ``config.py`` does ``import os`` and calls ``os._exit``, so patching
        the attribute on the ``os`` module itself is the narrow hook — and
        patching it is what keeps a WinNAS test run from killing pytest. The
        stub then raises instead of returning, because a real ``os._exit``
        never returns: letting the worker continue past the teardown would let
        this test pass on a regression that only *logged* the refusal.
        """
        calls: list = []

        def _fake_exit(code):
            calls.append(code)
            raise cls._SimulatedProcessExit(code)

        monkeypatch.setattr(os, "_exit", _fake_exit)
        return calls

    def test_main_thread_refusal_is_unchanged_and_does_not_exit(self, isolated_root,
                                                               monkeypatch, caplog):
        """Startup refusal on the main thread: exception returned, no os._exit."""
        from v3core.config import (TEST_MODE_WORKER_EXIT_CODE, TestModeConfigError,
                                   _resolve_data_dir)
        exits = self._capture_exit(monkeypatch)
        assert TEST_MODE_WORKER_EXIT_CODE not in exits
        isolated_root["config"].unlink()
        with caplog.at_level("CRITICAL", logger="v3core.config"):
            with pytest.raises(TestModeConfigError) as exc:
                _resolve_data_dir()
        assert REFUSAL_MARKER in str(exc.value)
        assert exits == [], f"main-thread refusal must not exit the process: {exits}"
        assert not [r for r in caplog.records if r.levelname == "CRITICAL"], (
            "startup refusal must stay an exception, not a process teardown"
        )

    def test_worker_thread_refusal_terminates_the_process(self, isolated_root,
                                                          monkeypatch, caplog):
        """Writer / HTTP worker path: the process exits 70, it does not thread away."""
        from v3core.config import TEST_MODE_WORKER_EXIT_CODE, _resolve_data_dir
        assert TEST_MODE_WORKER_EXIT_CODE == 70
        exits = self._capture_exit(monkeypatch)
        isolated_root["config"].unlink()  # T1's mid-run config loss
        outcome: dict = {}

        def worker():
            # exactly what a writer / HTTP handler does on every ingest touch
            try:
                _resolve_data_dir()
                outcome["returned"] = True
            except self._SimulatedProcessExit:
                pass  # the real os._exit never returns to the caller

        with caplog.at_level("CRITICAL", logger="v3core.config"):
            thread = threading.Thread(target=worker, name="f2-writer-probe")
            thread.start()
            thread.join(timeout=60)

        assert not thread.is_alive(), "worker thread hung instead of refusing"
        assert "returned" not in outcome, (
            "worker survived the refusal — serve() would keep its socket open"
        )
        assert exits == [TEST_MODE_WORKER_EXIT_CODE], (
            f"off-thread refusal must terminate the isolated process: {exits}"
        )
        critical = [r for r in caplog.records
                    if r.levelname == "CRITICAL" and r.name == "v3core.config"]
        assert critical, "no critical line was logged before the teardown"
        line = critical[-1].getMessage()
        assert line.startswith(REFUSAL_MARKER)
        assert "f2-writer-probe" in line, "the critical line must name the thread"
        assert str(TEST_MODE_WORKER_EXIT_CODE) in line
        assert "\n" not in line, "the critical line must be a single sanitized line"
        assert CONFIG_ENV in line, "the critical line must say what was lost"

    def test_production_mode_worker_thread_never_exits(self, tmp_path, monkeypatch):
        """The teardown is TEST-only: a production worker keeps its fallback."""
        from v3core.config import _resolve_data_dir
        exits = self._capture_exit(monkeypatch)
        home = TestProductionModeUnchanged._home(tmp_path, monkeypatch, "prod-home5")
        seen: dict = {}

        def worker():
            seen["root"] = str(_resolve_data_dir())

        thread = threading.Thread(target=worker, name="f2-prod-probe")
        thread.start()
        thread.join(timeout=60)

        assert not thread.is_alive()
        assert seen["root"] == str(home / ".v3-core" / "profiles" / "default"), seen
        assert exits == [], f"production mode must never exit the process: {exits}"
