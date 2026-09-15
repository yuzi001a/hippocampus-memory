# -*- coding: utf-8 -*-
"""Tests for the G6C-A LoCoMo Recall V2 durable CLI surface.

These tests cover, without ever calling provider / PG / HTTP:

  * argument-parser shape (``--dataset`` /
    ``--expected-sha256`` / ``--dsn`` / ``--output-dir`` /
    ``--cache-dir`` / ``--source-config`` /
    ``--batch-size`` / ``--mode`` / ``--case-limit`` /
    ``--sample-filter`` / ``--rerank`` / ``--repeatability``);
  * ``redact_secrets_in_payload`` strips ``password=...`` /
    ``api_key=...`` / ``token=...`` / ``secret=...`` strings;
  * ``load_source_config_embed_section`` only accepts the
    ``storage.embed`` sub-dict, ignores benign unrelated
    top-level keys (``basePath``, ``pg``, ``rerank``,
    ``prompts``, …), and refuses credential-shaped
    top-level keys (and credential-shaped nested keys in
    ``storage.embed``);
  * ``build_isolated_lab_config`` requires a real empty
    disposable experiment directory (NOT an empty string,
    NOT a fallback), validates the loopback DSN, rejects
    credential-shaped keys, and exposes the borrowed ephemeral
    provider config under ``storage.embed``;
  * ``run_dry_run`` verifies source SHA, computes volume /
    manifest, builds rows, and makes ZERO provider / PG /
    HTTP calls — the manifest on disk is sanitised and
    stamps the source-config SHA;
  * ``run_semantic`` exposes the cross-module seams it
    touches and raises :class:`IntegrationTODOError` when a
    required seam is missing (so the CLI surfaces exit 2
    rather than fabricating a degraded path);
  * ``main()`` exits 0 on a successful dry-run, 1 on a
    bad SHA / refused DSN / forbidden source-config key,
    and 2 on a forced integration gap.

No test ever reaches a real provider / PG / HTTP. No test
contains a real secret-shaped literal — the credential-shaped
strings the tests assert against are simple test fixtures
clearly named ``PLACEHOLDER_API_KEY`` etc.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import os
import sys
import tempfile

import pytest
import yaml


# ---------------------------------------------------------------------
# Path setup — mirror the other tests in this package
# ---------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from eval.locomo_recall_v2 import cli  # noqa: E402
from eval.locomo_recall_v2 import dataset as ds  # noqa: E402


# ---------------------------------------------------------------------
# Fixture: a tiny LoCoMo dump with deterministic SHA
# ---------------------------------------------------------------------


def _write_locomo(path: str, payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with open(path, "wb") as f:
        f.write(blob)
    return hashlib.sha256(blob).hexdigest()


def _basic_locomo() -> dict:
    return {
        "samples": [
            {
                "sample_id": "s1",
                "conversation": {
                    "session_1": {
                        "date_time": "2024-01-01 10:00:00",
                        "messages": [
                            {"dia_id": "d_1", "speaker": "speaker_1",
                             "text": "Hello there"},
                            {"dia_id": "d_2", "speaker": "speaker_2",
                             "text": "Hi! How are you?"},
                            {"dia_id": "d_3", "speaker": "speaker_1",
                             "text": "Doing well, thanks."},
                            {"dia_id": "d_4", "speaker": "speaker_2",
                             "text": "Glad to hear."},
                        ],
                    },
                },
                "qa": [
                    {
                        "id": "d_3",
                        "question": "How are you?",
                        "answer": "Doing well, thanks.",
                        "category": "social",
                        "evidence": ["d_3"],
                    },
                ],
            },
        ],
    }


def _write_source_config(
    path: str,
    *,
    embed: dict | None,
    extra_top_level: dict | None = None,
) -> str:
    payload: dict = {}
    if embed is not None:
        payload["storage"] = {"embed": embed}
    if extra_top_level:
        payload.update(extra_top_level)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f)
    return path


GOOD_DSN = "host=127.0.0.1 port=55465 dbname=lab user=lab password=PLACEHOLDER_LAB_PASS"


def _make_empty_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    # Make sure it's empty (defensive).
    for f in os.listdir(path):
        os.unlink(os.path.join(path, f))
    return path


# ---------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------


class TestArgumentParser:
    def test_parser_builds_and_requires_dataset(self):
        p = cli.make_argument_parser()
        ns = p.parse_args([])
        assert ns.command is None

        with pytest.raises(SystemExit):
            p.parse_args(["run"])

    def test_parser_accepts_full_command_line(self, tmp_path):
        p = cli.make_argument_parser()
        ns = p.parse_args([
            "run",
            "--dataset", "/tmp/d.json",
            "--expected-sha256", "0" * 64,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(tmp_path / "cfg.yaml"),
            "--mode", "dry-run",
            "--batch-size", "16",
            "--case-limit", "5",
            "--sample-filter", "s1,s2",
        ])
        assert ns.command == "run"
        assert ns.dataset == "/tmp/d.json"
        assert ns.mode == "dry-run"
        assert ns.batch_size == 16
        assert ns.case_limit == 5
        assert ns.sample_filter == "s1,s2"
        assert ns.rerank is False  # default

    def test_parser_rejects_unknown_mode(self):
        p = cli.make_argument_parser()
        with pytest.raises(SystemExit):
            p.parse_args([
                "run", "--dataset", "/tmp/d.json",
                "--expected-sha256", "0" * 64,
                "--dsn", GOOD_DSN,
                "--output-dir", "/tmp/out",
                "--cache-dir", "/tmp/cache",
                "--source-config", "/tmp/cfg.yaml",
                "--mode", "structural",  # legacy mode name
            ])

    def test_parser_rerank_flag(self):
        p = cli.make_argument_parser()
        ns = p.parse_args([
            "run", "--dataset", "/tmp/d.json",
            "--expected-sha256", "0" * 64,
            "--dsn", GOOD_DSN,
            "--output-dir", "/tmp/out",
            "--cache-dir", "/tmp/cache",
            "--source-config", "/tmp/cfg.yaml",
            "--rerank",
            "--rerank-endpoint-env", "LOCOMO_RERANK_URL",
        ])
        assert ns.rerank is True
        assert ns.rerank_endpoint_env == "LOCOMO_RERANK_URL"


# ---------------------------------------------------------------------
# redact_secrets_in_payload
# ---------------------------------------------------------------------


class TestRedactSecrets:
    def test_redacts_password_in_dsn(self):
        out = cli.redact_secrets_in_payload(
            "host=127.0.0.1 dbname=lab user=lab password=PLACEHOLDER_LAB_PASS"
        )
        assert "PLACEHOLDER_LAB_PASS" not in str(out)
        assert "***" in str(out)

    def test_redacts_in_nested_dict(self):
        payload = {
            "dsn": "host=127.0.0.1 password=hunter2 dbname=lab",
            "nested": {
                "url": "https://api.example.com?api_key=PLACEHOLDER_KEY",
                "inner_list": ["password=PLACEHOLDER_PASS", "no secret here"],
            },
        }
        redacted = cli.redact_secrets_in_payload(payload)
        blob = json.dumps(redacted)
        assert "hunter2" not in blob
        assert "PLACEHOLDER_KEY" not in blob
        assert "PLACEHOLDER_PASS" not in blob

    def test_does_not_mutate_input(self):
        payload = {"dsn": "host=x password=PLACEHOLDER"}
        _ = cli.redact_secrets_in_payload(payload)
        assert payload["dsn"] == "host=x password=PLACEHOLDER"

    def test_passes_through_non_strings(self):
        assert cli.redact_secrets_in_payload(42) == 42
        assert cli.redact_secrets_in_payload(True) is True
        assert cli.redact_secrets_in_payload(None) is None

    def test_handles_credentialless_strings(self):
        assert (
            cli.redact_secrets_in_payload("no secrets here")
            == "no secrets here"
        )


# ---------------------------------------------------------------------
# load_source_config_embed_section — strict read-only loader
# ---------------------------------------------------------------------


class TestLoadSourceConfigEmbedSection:
    def test_loads_embed_section_only(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(cfg, embed={
            "endpoint": "https://api.example.com/v1/embeddings",
            "model": "BAAI/bge-m3",
            "dim": 1024,
        })
        out = cli.load_source_config_embed_section(str(cfg))
        assert out["endpoint"].startswith("https://")
        assert out["model"] == "BAAI/bge-m3"
        assert out["dim"] == 1024

    def test_ignores_benign_top_level_keys(self, tmp_path):
        """Benign unrelated top-level keys (``basePath``,
        ``pg``, ``rerank``, ``prompts``, …) are read read-only
        and ignored — the CLI only consumes ``storage.embed``.
        """
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(
            cfg, embed={"endpoint": "https://x", "model": "m"},
            extra_top_level={
                "basePath": "/var/data/locomo",
                "pg": {"host": "127.0.0.1", "port": 5432},
                "rerank": {"enabled": True},
                "prompts": ["q1", "q2"],
            },
        )
        out = cli.load_source_config_embed_section(str(cfg))
        # Only storage.embed is returned.
        assert out == {"endpoint": "https://x", "model": "m"}

    def test_ignores_benign_basepath_only(self, tmp_path):
        """A bare top-level ``basePath`` (no storage.embed at
        all) is ignored and the loader still refuses because
        ``storage`` is missing — benign keys are NOT a substitute
        for the required ``storage.embed`` payload.
        """
        cfg = tmp_path / "cfg.yaml"
        with open(cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump({"basePath": "/var/data/locomo"}, f)
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_top_level_password(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(
            cfg, embed={"endpoint": "https://x", "model": "m"},
            extra_top_level={"password": "PLACEHOLDER_PASSWORD"},
        )
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_top_level_api_key(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(
            cfg, embed={"endpoint": "https://x", "model": "m"},
            extra_top_level={"api_key": "PLACEHOLDER_API_KEY"},
        )
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_top_level_token(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(
            cfg, embed={"endpoint": "https://x", "model": "m"},
            extra_top_level={"access_token": "PLACEHOLDER_TOKEN"},
        )
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_top_level_production_dsn(self, tmp_path):
        """Credential-shaped top-level keys still refuse."""
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(
            cfg, embed={"endpoint": "https://x", "model": "m"},
            extra_top_level={"production_db_dsn": "x"},
        )
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_missing_storage(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        with open(cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump({"foo": "bar"}, f)
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_empty_storage(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        with open(cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump({"storage": {}}, f)
        # An empty embed section is allowed; the semantic
        # stage will then refuse to make provider calls.
        assert cli.load_source_config_embed_section(str(cfg)) == {}

    def test_accepts_api_key_in_embed_section(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(cfg, embed={
            "endpoint": "https://x", "model": "m",
            "api_key": "PLACEHOLDER_API_KEY",
        })
        # The exact lowercase ``api_key`` is now accepted inside
        # ``storage.embed`` (borrowed ephemeral provider key,
        # in-memory only).
        embed_section = cli.load_source_config_embed_section(str(cfg))
        assert embed_section["api_key"] == "PLACEHOLDER_API_KEY"
        # Profile-shaped serializer strips api_key / apiKey /
        # endpoint / proxy / _raw from the JSON-safe profile.
        safe = cli._strip_endpoint_from_profile(embed_section)
        assert "api_key" not in safe
        assert "apiKey" not in safe
        assert "PLACEHOLDER_API_KEY" not in json.dumps(safe)

    def test_refuses_password_in_embed(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        _write_source_config(cfg, embed={
            "endpoint": "https://x", "model": "m",
            "password": "PLACEHOLDER_PASSWORD",
        })
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_missing_file(self, tmp_path):
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(tmp_path / "nope.yaml"))

    def test_refuses_malformed_yaml(self, tmp_path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(": not valid yaml:", encoding="utf-8")
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))

    def test_refuses_oversized_file(self, tmp_path):
        cfg = tmp_path / "huge.yaml"
        # 64 KiB + 1 byte of valid YAML.
        cfg.write_text(
            "storage:\n  embed:\n    blob: \"" + "a" * (cli._MAX_SOURCE_CONFIG_BYTES + 1) + "\"\n",
            encoding="utf-8",
        )
        with pytest.raises(cli.SourceConfigRefused):
            cli.load_source_config_embed_section(str(cfg))


# ---------------------------------------------------------------------
# build_isolated_lab_config — strict safety contract
# ---------------------------------------------------------------------


class TestIsolatedLabConfig:
    def _base_path(self, tmp_path) -> str:
        return _make_empty_dir(str(tmp_path / "experiment-base"))

    def test_base_path_must_be_real_dir_not_empty_string(self, tmp_path):
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path="",
                dsn=GOOD_DSN,
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_base_path_must_exist(self, tmp_path):
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path=str(tmp_path / "does-not-exist"),
                dsn=GOOD_DSN,
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_base_path_must_be_empty(self, tmp_path):
        bp = self._base_path(tmp_path)
        # Pollute it.
        with open(os.path.join(bp, "stale.txt"), "w", encoding="utf-8") as f:
            f.write("stale")
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path=bp,
                dsn=GOOD_DSN,
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_carries_storage_pg_dsn(self, tmp_path):
        bp = self._base_path(tmp_path)
        cfg = cli.build_isolated_lab_config(
            base_path=bp,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            embed_cfg={"endpoint": "https://x", "model": "m"},
        )
        assert cfg["lab"] is True
        # ``v3core.pg_store.PgEmbedStore`` reads ``storage.pg`` as
        # psycopg2 kwargs; a bare ``dsn`` key would be ignored and the
        # store would silently fall back to its own defaults
        # (localhost:5433 = production). The isolated config therefore
        # spells out every field explicitly.
        pg = cfg["storage"]["pg"]
        assert pg["host"] == "127.0.0.1"
        assert pg["port"] == 55465
        assert pg["database"] == "lab"
        assert pg["user"] == "lab"
        assert "dsn" not in pg
        assert pg["password"] == "PLACEHOLDER_LAB_PASS"

    def test_refuses_dsn_without_explicit_port(self, tmp_path):
        """A portless lab DSN is refused so the store can never fall
        back to the default (production) port."""
        bp = self._base_path(tmp_path)
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path=bp,
                dsn="host=127.0.0.1 dbname=lab user=lab password=x",
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_carries_storage_embed_section(self, tmp_path):
        bp = self._base_path(tmp_path)
        cfg = cli.build_isolated_lab_config(
            base_path=bp,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            embed_cfg={"endpoint": "https://x", "model": "BAAI/bge-m3", "dim": 1024},
        )
        assert cfg["storage"]["embed"]["model"] == "BAAI/bge-m3"
        assert cfg["storage"]["embed"]["dim"] == 1024

    def test_rerank_disabled_by_default(self, tmp_path):
        bp = self._base_path(tmp_path)
        cfg = cli.build_isolated_lab_config(
            base_path=bp,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            embed_cfg={"endpoint": "https://x", "model": "m"},
        )
        assert cfg["rerank_enabled"] is False

    def test_rerank_enabled_when_requested(self, tmp_path):
        bp = self._base_path(tmp_path)
        cfg = cli.build_isolated_lab_config(
            base_path=bp,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            embed_cfg={"endpoint": "https://x", "model": "m"},
            rerank=True,
            rerank_endpoint_env="LOCOMO_RERANK_URL",
        )
        assert cfg["rerank_enabled"] is True
        assert cfg["rerank_endpoint_env"] == "LOCOMO_RERANK_URL"

    def test_refuses_non_loopback_dsn(self, tmp_path):
        bp = self._base_path(tmp_path)
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path=bp,
                dsn="host=db.internal port=55465 dbname=lab user=lab",
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_refuses_reserved_production_dsn(self, tmp_path):
        bp = self._base_path(tmp_path)
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path=bp,
                dsn="host=127.0.0.1 port=5433 dbname=lab user=lab",
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_refuses_empty_dsn(self, tmp_path):
        bp = self._base_path(tmp_path)
        with pytest.raises(cli.LabConfigRefused):
            cli.build_isolated_lab_config(
                base_path=bp,
                dsn="",
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                embed_cfg={"endpoint": "https://x", "model": "m"},
            )

    def test_accepts_api_key_in_embed_storage(self, tmp_path):
        bp = self._base_path(tmp_path)
        cfg = cli.build_isolated_lab_config(
            base_path=bp,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            embed_cfg={
                "endpoint": "https://x", "model": "m",
                "api_key": "PLACEHOLDER",
            },
        )
        # The exact lowercase ``api_key`` is accepted inside
        # ``storage.embed`` (borrowed ephemeral provider key,
        # in-memory only) and absent from the JSON-safe profile.
        assert cfg["storage"]["embed"]["api_key"] == "PLACEHOLDER"
        safe = cli._strip_endpoint_from_profile(cfg["storage"]["embed"])
        assert "api_key" not in safe
        assert "PLACEHOLDER" not in json.dumps(safe)

    def test_no_secrets_or_passwords_in_config(self, tmp_path):
        bp = self._base_path(tmp_path)
        cfg = cli.build_isolated_lab_config(
            base_path=bp,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            embed_cfg={"endpoint": "https://x", "model": "m"},
            rerank=True,
            rerank_endpoint_env="LOCOMO_RERANK_URL",
        )
        blob = json.dumps(cfg)
        # The lab config carries the loopback lab DSN (which
        # has been validated) — the contract forbids RAW
        # secret-shaped values like ``sk-...`` / API keys /
        # tokens. The embed section must not carry an api_key
        # either.
        assert "sk-" not in blob
        assert "PLACEHOLDER_API_KEY" not in blob
        # Rerank endpoint is an env-var NAME; raw URL/key is forbidden.
        assert "https://" not in blob or "endpoint" in blob
        # storage.embed should NOT contain an api_key.
        assert "api_key" not in cfg["storage"]["embed"]
        assert "apiKey" not in cfg["storage"]["embed"]


# ---------------------------------------------------------------------
# run_dry_run — full end-to-end on a fixture, no PG / no provider
# ---------------------------------------------------------------------


class TestRunDryRun:
    def _write_fixture(self, tmp_path):
        locomo_path = tmp_path / "locomo.json"
        digest = _write_locomo(str(locomo_path), _basic_locomo())
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={
            "endpoint": "https://api.example.com/v1/embeddings",
            "model": "BAAI/bge-m3",
            "dim": 1024,
        })
        return locomo_path, digest, cfg_path

    def test_end_to_end_writes_manifest_and_rows(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        out_dir = tmp_path / "out"
        cache_dir = tmp_path / "cache"

        result = cli.run_dry_run(
            dataset_path=str(locomo_path),
            expected_sha256=digest,
            dsn=GOOD_DSN,
            output_dir=str(out_dir),
            cache_dir=str(cache_dir),
            source_config_path=str(cfg_path),
            rerank=False,
            rerank_endpoint_env="",
            write_rows=True,
            commit_sha="",
        )

        assert isinstance(result, cli.StructuralResult)
        assert result.dataset_sha256 == digest
        assert result.sample_count == 1
        assert result.eval_row_count == 1
        assert result.qa_pair_count >= 1

        # Manifest on disk + sanitised.
        manifest_path = out_dir / "benchmark-manifest.json"
        assert manifest_path.is_file()
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert m["source"]["sha256"] == digest
        assert m["provider_id"] == "siliconflow"
        assert m["model_id"] == "BAAI/bge-m3"
        assert m["embedding_dim"] == 1024
        assert m["source_config_sha256"]

        # Import rows on disk.
        rows_path = out_dir / "import-rows.json"
        assert rows_path.is_file()
        rows = json.loads(rows_path.read_text(encoding="utf-8"))
        assert "qa_pairs" in rows
        assert "conversation_stream" in rows
        assert "eval_queries" in rows

        # The lab config was built.
        assert result.lab_config["basePath"]
        assert os.path.isdir(result.lab_config["basePath"])
        pg = result.lab_config["storage"]["pg"]
        assert pg["host"] == "127.0.0.1"
        assert pg["port"] == 55465
        assert pg["database"] == "lab"

    def test_manifest_carries_no_secrets(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        result = cli.run_dry_run(
            dataset_path=str(locomo_path),
            expected_sha256=digest,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            source_config_path=str(cfg_path),
            rerank=False,
            rerank_endpoint_env="",
            write_rows=False,
        )
        blob = json.dumps(result.manifest)
        assert "PLACEHOLDER_LAB_PASS" not in blob
        assert "127.0.0.1" not in blob
        assert "55465" not in blob

    def test_wrong_sha_raises(self, tmp_path):
        locomo_path, _digest, cfg_path = self._write_fixture(tmp_path)
        with pytest.raises(ds.LoCoMoSourceError):
            cli.run_dry_run(
                dataset_path=str(locomo_path),
                expected_sha256="0" * 64,
                dsn=GOOD_DSN,
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                source_config_path=str(cfg_path),
                rerank=False,
                rerank_endpoint_env="",
                write_rows=False,
            )

    def test_missing_dataset_raises(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={"endpoint": "x", "model": "m"})
        with pytest.raises(ds.LoCoMoSourceError):
            cli.run_dry_run(
                dataset_path=str(tmp_path / "no-such.json"),
                expected_sha256="0" * 64,
                dsn=GOOD_DSN,
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                source_config_path=str(cfg_path),
                rerank=False,
                rerank_endpoint_env="",
                write_rows=False,
            )

    def test_refused_dsn_raises(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        with pytest.raises(cli.LabConfigRefused):
            cli.run_dry_run(
                dataset_path=str(locomo_path),
                expected_sha256=digest,
                dsn="host=db.internal port=55465 dbname=lab",
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                source_config_path=str(cfg_path),
                rerank=False,
                rerank_endpoint_env="",
                write_rows=False,
            )

    def test_forbidden_source_config_key_raises(self, tmp_path):
        locomo_path, digest, _cfg_path = self._write_fixture(tmp_path)
        bad_cfg = tmp_path / "bad.yaml"
        with pytest.raises(cli.SourceConfigRefused):
            _write_source_config(
                bad_cfg, embed={
                    "endpoint": "https://x",
                    "model": "m",
                    "password": "PLACEHOLDER_PASSWORD",
                },
            )
            cli.run_dry_run(
                dataset_path=str(locomo_path),
                expected_sha256=digest,
                dsn=GOOD_DSN,
                output_dir=str(tmp_path / "out"),
                cache_dir=str(tmp_path / "cache"),
                source_config_path=str(bad_cfg),
                rerank=False,
                rerank_endpoint_env="",
                write_rows=False,
            )

    def test_case_limit_narrows_eval_rows(self, tmp_path):
        # Build a dataset with multiple eval rows.
        payload = _basic_locomo()
        payload["samples"][0]["qa"].append({
            "id": "d_4",
            "question": "What?",
            "answer": "Glad to hear.",
            "category": "social",
            "evidence": ["d_4"],
        })
        locomo_path = tmp_path / "locomo.json"
        digest = _write_locomo(str(locomo_path), payload)
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={"endpoint": "x", "model": "m"})

        result = cli.run_dry_run(
            dataset_path=str(locomo_path),
            expected_sha256=digest,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            source_config_path=str(cfg_path),
            rerank=False,
            rerank_endpoint_env="",
            write_rows=False,
            case_limit=1,
        )
        assert result.eval_row_count == 1
        assert result.manifest["case_limit"] == 1

    def test_sample_filter_narrows_eval_rows(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        result = cli.run_dry_run(
            dataset_path=str(locomo_path),
            expected_sha256=digest,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            source_config_path=str(cfg_path),
            rerank=False,
            rerank_endpoint_env="",
            write_rows=False,
            sample_filter=("s1",),
        )
        assert result.manifest["filtered_sample_ids"] == ["s1"]
        assert result.eval_row_count == 1

    def test_zero_provider_pg_calls_during_dry_run(self, tmp_path, monkeypatch):
        """Dry-run MUST NOT call into the embeddings module or psycopg2.

        We patch the embeddings entry points to raise so any
        accidental invocation is loud. We also patch the lab
        bootstrap / import_rows to raise so any accidental PG
        call is loud.
        """

        from eval.locomo_recall_v2 import embeddings as emb
        from eval.locomo_recall_v2 import lab as _lab

        def _explode(*a, **kw):
            raise AssertionError("dry-run invoked embeddings API")

        monkeypatch.setattr(emb, "prepare_query_embeddings", _explode)
        monkeypatch.setattr(emb, "prepare_corpus_embeddings", _explode)

        def _explode_lab(*a, **kw):
            raise AssertionError("dry-run invoked lab.bootstrap_schema")

        monkeypatch.setattr(_lab, "bootstrap_schema", _explode_lab)
        monkeypatch.setattr(_lab, "import_rows", _explode_lab)

        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        cli.run_dry_run(
            dataset_path=str(locomo_path),
            expected_sha256=digest,
            dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"),
            cache_dir=str(tmp_path / "cache"),
            source_config_path=str(cfg_path),
            rerank=False,
            rerank_endpoint_env="",
            write_rows=False,
        )


# ---------------------------------------------------------------------
# main() — full CLI driver
# ---------------------------------------------------------------------


class TestMainDriver:
    def _write_fixture(self, tmp_path):
        locomo_path = tmp_path / "locomo.json"
        digest = _write_locomo(str(locomo_path), _basic_locomo())
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={
            "endpoint": "https://api.example.com/v1/embeddings",
            "model": "BAAI/bge-m3",
            "dim": 1024,
        })
        return locomo_path, digest, cfg_path

    def test_no_command_prints_help_and_exits_1(self):
        rc = cli.main([])
        assert rc == cli.EXIT_INPUT_ERROR

    def test_dry_run_succeeds(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        out_dir = tmp_path / "out"
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(out_dir),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "dry-run",
            "--write-rows",
        ])
        assert rc == cli.EXIT_OK
        assert (out_dir / "benchmark-manifest.json").is_file()

    def test_wrong_sha_returns_exit_1(self, tmp_path):
        locomo_path, _digest, cfg_path = self._write_fixture(tmp_path)
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", "0" * 64,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "dry-run",
        ])
        assert rc == cli.EXIT_INPUT_ERROR

    def test_refused_dsn_returns_exit_1(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", "host=db.internal port=55465 dbname=lab",
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "dry-run",
        ])
        assert rc == cli.EXIT_INPUT_ERROR

    def test_forbidden_source_config_key_returns_exit_1(self, tmp_path):
        locomo_path, digest, _cfg_path = self._write_fixture(tmp_path)
        bad_cfg = tmp_path / "bad.yaml"
        _write_source_config(
            bad_cfg, embed={
                "endpoint": "https://x", "model": "m",
                "password": "PLACEHOLDER_PASSWORD",
            },
        )
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(bad_cfg),
            "--mode", "dry-run",
        ])
        assert rc == cli.EXIT_INPUT_ERROR

    def test_rerank_without_endpoint_env_returns_exit_1(self, tmp_path):
        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "dry-run",
            "--rerank",
        ])
        assert rc == cli.EXIT_INPUT_ERROR


# ---------------------------------------------------------------------
# run_semantic — focused dry-run / integration surface tests
# ---------------------------------------------------------------------


class TestRunSemanticIntegration:
    """Focused tests for the semantic stage. We never open a real
    PG connection — the only seam we exercise here is the
    embedding-preparation contract.
    """

    def _write_fixture(self, tmp_path):
        locomo_path = tmp_path / "locomo.json"
        digest = _write_locomo(str(locomo_path), _basic_locomo())
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={
            "endpoint": "https://api.example.com/v1/embeddings",
            "model": "BAAI/bge-m3",
            "dim": 1024,
        })
        return locomo_path, digest, cfg_path

    def test_surfaces_integration_todo_when_embeddings_unavailable(
        self, tmp_path, monkeypatch
    ):
        """When the embedding-preparation seam is missing, the
        semantic stage must raise ``IntegrationTODOError``.

        We force the failure by patching
        :func:`cli._run_query_embed_pass` to raise the typed
        error so the CLI surfaces exit 2 rather than fabricating
        a degraded path.
        """

        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
        from eval.locomo_recall_v2 import cli as _cli
        # Stub the lab connect so no real DSN is opened.
        # The G6C-B0 disposable-DSN preflight now also calls
        # ``_connect_lab(dsn)``; return a stub whose cursor
        # reports ``ivfflat.probes`` as REGISTERED so the
        # preflight is a no-op (default ANN policy = no
        # explicit probes, so the AVAILABLE verdict does
        # not refuse).
        class _StubCursor:
            def __init__(self):
                self._pending = None
            def execute(self, sql, params=None):
                # Pretend pg_settings has a row for
                # ``ivfflat.probes`` — registered.
                self._pending = (1,)
            def fetchone(self):
                return self._pending
            def close(self):
                pass

        class _StubConn:
            def cursor(self_inner):
                return _StubCursor()
            def close(self_inner):
                pass

        monkeypatch.setattr(_cli, "_connect_lab", lambda dsn: _StubConn())
        # Stub the lab bootstrap + import_rows so no PG is touched.
        from eval.locomo_recall_v2 import lab as _lab
        monkeypatch.setattr(_lab, "bootstrap_schema", lambda *a, **kw: None)
        monkeypatch.setattr(_lab, "import_rows", lambda *a, **kw: {
            "qa_pairs": [], "conversation_stream": [], "eval_queries": [],
        })
        # The corpus-vector pass runs before the query pass; stub it so
        # this test never reaches the provider, and stub the provenance
        # read-back so the stubbed (None) connection is never used.
        monkeypatch.setattr(
            _cli, "_prepare_corpus_vectors", lambda **kw: None,
        )
        monkeypatch.setattr(
            _cli, "_read_back_provenance", lambda conn: ({}, []),
        )
        monkeypatch.setattr(
            _cli, "_run_query_embed_pass",
            lambda **kw: (_ for _ in ()).throw(
                _cli.IntegrationTODOError("forced"),
            ),
        )
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
        ])
        assert rc == cli.EXIT_INTEGRATION_GAP

    def test_search_mode_exact_with_explicit_probes_is_refused(
        self, tmp_path, monkeypatch
    ):
        """``--search-mode exact --ivfflat-probes N`` is a
        contradictory contract — exact mode MUST NOT pin planner
        probe decisions. The CLI MUST reject this combination
        BEFORE any DB bootstrap / import_rows / corpus-vector
        prep so the disposable lab tree is never touched by a
        run that would fail closed anyway. Without this guard the
        contradiction only surfaces at ``apply_search_protocol``
        lease-verify time, after bootstrap has run.
        """

        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)

        # Sentinel counters — if either fires the policy guard
        # did NOT close the seam early enough and bootstrap /
        # import_rows was reached on a contradictory policy.
        from eval.locomo_recall_v2 import cli as _cli
        from eval.locomo_recall_v2 import lab as _lab
        bootstrap_calls: list[bool] = []
        import_calls: list[bool] = []

        def _boom_bootstrap(*a, **kw):
            bootstrap_calls.append(True)
            raise AssertionError(
                "bootstrap_schema reached on contradictory "
                "exact+probes policy — policy guard is too late"
            )

        def _boom_import_rows(*a, **kw):
            import_calls.append(True)
            raise AssertionError(
                "import_rows reached on contradictory "
                "exact+probes policy — policy guard is too late"
            )

        monkeypatch.setattr(_cli, "_connect_lab", lambda dsn: None)
        monkeypatch.setattr(_lab, "bootstrap_schema", _boom_bootstrap)
        monkeypatch.setattr(_lab, "import_rows", _boom_import_rows)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--search-mode", "exact",
            "--ivfflat-probes", "4",
        ])
        assert rc == cli.EXIT_INPUT_ERROR
        assert bootstrap_calls == [], (
            "policy guard let the run reach bootstrap_schema"
        )
        assert import_calls == [], (
            "policy guard let the run reach import_rows"
        )

    def test_explicit_probes_with_unregistered_guc_is_refused_before_bootstrap(
        self, tmp_path, monkeypatch
    ):
        """``--search-mode ann --ivfflat-probes N`` against a
        disposable lab whose ``pg_settings`` carries no row
        for ``ivfflat.probes`` MUST be refused BEFORE
        ``bootstrap_schema`` / ``import_rows`` runs.

        Without this preflight the contract violation (a
        custom-GUC echo of the requested value) would only
        surface at ``apply_search_protocol`` lease-verify
        time, AFTER the lab is touched and the disposable
        tree is half-populated.
        """

        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)

        from eval.locomo_recall_v2 import cli as _cli
        from eval.locomo_recall_v2 import lab as _lab

        bootstrap_calls: list[bool] = []
        import_calls: list[bool] = []

        def _boom_bootstrap(*a, **kw):
            bootstrap_calls.append(True)
            raise AssertionError(
                "bootstrap_schema reached on unregistered "
                "ivfflat.probes — preflight is too late"
            )

        def _boom_import_rows(*a, **kw):
            import_calls.append(True)
            raise AssertionError(
                "import_rows reached on unregistered "
                "ivfflat.probes — preflight is too late"
            )

        # The preflight calls ``_connect_lab(dsn)`` exactly
        # once and runs ``SELECT 1 FROM pg_settings WHERE
        # name = 'ivfflat.probes'`` against the disposable
        # DSN.  We monkey-patch ``_connect_lab`` with a fake
        # that returns a cursor whose ``fetchone`` is
        # ``None`` — i.e. no row in ``pg_settings`` for
        # ``ivfflat.probes`` (the disposable pgvector 0.8.6
        # condition).
        class _PreflightCursor:
            def __init__(self, _conn):
                self._pending = None
                self.log = []

            def execute(self, sql, params=None):
                self.log.append((str(sql), tuple(params) if params else ()))
                text = str(sql).lstrip().upper()
                if "FROM PG_SETTINGS" in text:
                    # No row → UNAVAILABLE.
                    self._pending = None
                    return
                # Anything else is unexpected in the
                # preflight path.
                raise AssertionError(
                    f"_PreflightCursor unexpected: {sql!r}"
                )

            def fetchone(self):
                return self._pending

            def close(self):
                pass

        class _PreflightConn:
            def cursor(self_inner):
                return _PreflightCursor(self_inner)

            def close(self_inner):
                pass

        def _stub_connect_lab(dsn):
            return _PreflightConn()

        monkeypatch.setattr(_cli, "_connect_lab", _stub_connect_lab)
        monkeypatch.setattr(_lab, "bootstrap_schema", _boom_bootstrap)
        monkeypatch.setattr(_lab, "import_rows", _boom_import_rows)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--search-mode", "ann",
            "--ivfflat-probes", "1",
        ])
        assert rc == cli.EXIT_INPUT_ERROR, (
            "expected EXIT_INPUT_ERROR from preflight, "
            f"got rc={rc}"
        )
        assert bootstrap_calls == [], (
            "preflight let the run reach bootstrap_schema"
        )
        assert import_calls == [], (
            "preflight let the run reach import_rows"
        )

    def test_preflight_records_unavailable_for_implicit_probes(
        self, tmp_path, monkeypatch
    ):
        """Implicit ``--search-mode ann`` (no ``--ivfflat-probes``)
        against an unregistered GUC MUST NOT abort; the
        verdict is stamped as ``UNAVAILABLE`` and the run
        continues into the lab.

        Exact mode is a separate test class — exact mode +
        unregistered GUC is also tolerated and the auditor
        sees ``probe_availability='UNAVAILABLE'``.
        """

        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)

        from eval.locomo_recall_v2 import cli as _cli
        from eval.locomo_recall_v2 import embeddings as _embeddings

        class _UnavailableConn:
            def cursor(self_inner):
                class _Cur:
                    def __init__(s):
                        s._pending = None
                    def execute(s, sql, params=None):
                        s.text = str(sql).lstrip().upper()
                        s._pending = None
                    def fetchone(s):
                        return s._pending
                    def close(s):
                        pass
                return _Cur()
            def close(self_inner):
                pass

        monkeypatch.setattr(
            _cli, "_connect_lab", lambda dsn: _UnavailableConn()
        )
        # Stub the corpus-vector prep so we never reach a real
        # provider / HTTP.  We assert on the preflight verdict
        # being non-raising for implicit probes — that's the
        # whole contract under test.
        from eval.locomo_recall_v2 import lab as _lab

        class _StubStats:
            def to_dict(s):
                return {"calls": 0}

        monkeypatch.setattr(
            _cli, "_prepare_corpus_vectors",
            lambda **kw: _StubStats(),
        )
        monkeypatch.setattr(
            _embeddings, "prepare_query_embeddings",
            lambda **kw: ([], [], _StubStats()),
        )
        # After the preflight returns UNAVAILABLE without
        # raising, control flows into the lab.  Abort at the
        # very next seam (``bootstrap_schema``) — that proves
        # the preflight tolerated the implicit-probes case.
        seen_after_preflight: list[bool] = []

        def _capture_then_abort(*a, **kw):
            seen_after_preflight.append(True)
            raise _cli.IntegrationTODOError("forced-after-preflight")

        monkeypatch.setattr(_lab, "bootstrap_schema", _capture_then_abort)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--search-mode", "ann",
            # no --ivfflat-probes — implicit
        ])
        # The preflight does NOT raise for implicit probes;
        # the lab seam raises IntegrationTODOError → rc == 2.
        assert rc == cli.EXIT_INTEGRATION_GAP
        # Confirm we reached the post-preflight seam — i.e.
        # the preflight tolerated the implicit-probes case.
        assert seen_after_preflight == [True], (
            "preflight aborted implicit probes that should "
            "have been tolerated"
        )


# ---------------------------------------------------------------------
# Public API surface — names that callers may import
# ---------------------------------------------------------------------


class TestPublicAPI:
    def test_all_names_are_importable(self):
        for name in cli.__all__:
            assert hasattr(cli, name), f"missing public name: {name}"


# ---------------------------------------------------------------------
# Semantic-stage wiring — corpus vectors, objective mode, provenance
# ---------------------------------------------------------------------


class _FakeEmbedResult:
    """Minimal stand-in for ``embeddings.EmbedResult``."""

    def __init__(self, row_id, vector):
        self.row_id = row_id
        self.vector = vector
        self.cache_hit = False


class _FakeStats:
    def __init__(self):
        self.corpus_items = 0
        self.corpus_chars = 0
        self.query_items = 0
        self.query_chars = 0
        self.provider_batch_count = 0
        self.hits = 0
        self.misses = 0
        self.cache_root = ""

    def to_dict(self):
        return dict(self.__dict__)


class TestCorpusVectorWiring:
    """The semantic stage must attach REAL per-row vectors.

    These tests fake the provider seam so nothing leaves the process:
    ``prepare_corpus_embeddings`` is replaced with a deterministic
    function, and the assertions check the importer rows receive those
    vectors in the exact row order.
    """

    def _rows(self):
        return {
            "qa_pairs": [
                {"source_id": "locomo|eval_v2|s1|session_1|d1>d2",
                 "question": "Q1", "answer": "A1"},
                {"source_id": "locomo|eval_v2|s1|session_1|d3>d4",
                 "question": "Q2", "answer": "A2"},
            ],
            "conversation_stream": [
                {"content": "m0", "tool_calls": [
                    {"sample_id": "s1", "session_key": "session_1", "index": 0}]},
                {"content": "m1", "tool_calls": [
                    {"sample_id": "s1", "session_key": "session_1", "index": 1}]},
            ],
        }

    def test_corpus_rows_get_vectors_in_order(self, monkeypatch):
        rows = self._rows()
        seen = []

        def _fake_prepare(embed_rows, *, cfg, kind, cache_root,
                          dataset_sha, batch_size):
            seen.append((kind, [r.row_id for r in embed_rows]))
            results = [
                _FakeEmbedResult(r.row_id, [0.25 + i, 0.5, 0.75])
                for i, r in enumerate(embed_rows)
            ]
            return results, _FakeStats()

        from eval.locomo_recall_v2 import embeddings as _emb
        monkeypatch.setattr(_emb, "prepare_corpus_embeddings", _fake_prepare)

        cli._prepare_corpus_vectors(
            rows=rows["qa_pairs"], kind="qa_pairs",
            embed_cfg_input={"embed": {"endpoint": "x", "model": "m"}},
            cache_dir="unused", dataset_sha="0" * 64, batch_size=32,
        )
        cli._prepare_corpus_vectors(
            rows=rows["conversation_stream"], kind="conversation_stream",
            embed_cfg_input={"embed": {"endpoint": "x", "model": "m"}},
            cache_dir="unused", dataset_sha="0" * 64, batch_size=32,
        )

        # QA rows: row_id must be the canonical source_id, and the
        # vector must land on the matching row (not a shifted one).
        assert seen[0][0] == "qa_pairs"
        assert seen[0][1] == [
            "locomo|eval_v2|s1|session_1|d1>d2",
            "locomo|eval_v2|s1|session_1|d3>d4",
        ]
        assert rows["qa_pairs"][0]["embedding"] == [0.25, 0.5, 0.75]
        assert rows["qa_pairs"][1]["embedding"] == [1.25, 0.5, 0.75]
        # Conversation rows: synthesized stable row ids, same order.
        assert seen[1][0] == "conversation_stream"
        assert seen[1][1] == [
            "cs|locomo|s1|session_1|0",
            "cs|locomo|s1|session_1|1",
        ]
        assert rows["conversation_stream"][0]["embedding"] == [0.25, 0.5, 0.75]
        assert rows["conversation_stream"][1]["embedding"] == [1.25, 0.5, 0.75]

    def test_corpus_count_mismatch_fails_closed(self, monkeypatch):
        rows = self._rows()

        def _short_prepare(embed_rows, **kw):
            return [_FakeEmbedResult(embed_rows[0].row_id, [1.0])], _FakeStats()

        from eval.locomo_recall_v2 import embeddings as _emb
        monkeypatch.setattr(_emb, "prepare_corpus_embeddings", _short_prepare)
        with pytest.raises(cli.CLIError):
            cli._prepare_corpus_vectors(
                rows=rows["qa_pairs"], kind="qa_pairs",
                embed_cfg_input={"embed": {"endpoint": "x", "model": "m"}},
                cache_dir="unused", dataset_sha="0" * 64, batch_size=32,
            )


class TestRankingMapping:
    """Raw engine ids must resolve to canonical LoCoMo source ids."""

    def _record(self, **overrides):
        from eval.locomo_recall_v2 import adapter

        fields = dict(
            case_id="s|0", sample_id="s", query_idx=0, category="cat1",
            question="Q", answer="A", gold_evidence_dia_ids=(),
            gold_source_ids=(), unmapped_dia_ids=(), unresolved_evidence=(),
            context_block_length=0, trace_id="trace-x",
            ranked_source_ids=("qa_7",), selected_source_ids=("qa_7",),
            candidate_source_ids=("qa_7", "12"), injected_source_ids=("qa_7",),
            lane_summaries=(), injection_summary=None, drop_summary={},
            elapsed_ms=1.0, status="ok", error="",
        )
        fields.update(overrides)
        return adapter.CaseRecord(**fields)

    def test_mapping_applies_to_frozen_case_records(self):
        """Regression: ``CaseRecord`` is frozen, so the mapping must
        rebuild records (a swallowed ``FrozenInstanceError`` previously
        left every ranking in raw ``qa_<id>`` form and forced hit rates
        to zero by construction)."""

        rec = self._record()
        src_qa = "locomo|eval_v2|s|session_1|d1>d2"
        src_conv = "locomo|eval_v2|s|session_1|d3>d4"
        provenance_map = {
            "qa_id_to_source_id": {"qa_7": src_qa},
            "conv_id_to_source_id": {"12": src_conv},
            "topic_id_to_source_id": {},
        }
        out, audit = cli._map_record_rankings([rec], provenance_map)
        assert len(out) == 1
        mapped = out[0]
        assert mapped is not rec               # replaced, never mutated
        assert mapped.ranked_source_ids == (src_qa,)
        assert mapped.selected_source_ids == (src_qa,)
        assert mapped.injected_source_ids == (src_qa,)
        # candidate list carries both the qa id and the conversation id
        assert mapped.candidate_source_ids == (src_qa, src_conv)
        # the original record is untouched
        assert rec.ranked_source_ids == ("qa_7",)
        assert audit["resolved_qa"] >= 1
        assert audit["resolved_conv"] >= 1

    def test_unmapped_ids_are_dropped_with_an_audit_trail(self):
        rec = self._record(ranked_source_ids=("qa_999",))
        out, audit = cli._map_record_rankings([rec], {
            "qa_id_to_source_id": {}, "conv_id_to_source_id": {},
            "topic_id_to_source_id": {},
        })
        assert out[0].ranked_source_ids == ()
        assert audit["unresolved"] >= 1


class TestFacadePassWiring:
    """The facade pass must forward the isolated config + objective mode."""

    def test_query_row_key_parses_four_segment_query_ids(self):
        """Regression: the query namespace is ``locomo|eval_v2|s|qN``.

        Using the strict QA-pair parser here silently produced an empty
        per-case mapping, which the objective adapter then reported as
        ``invalid_q_emb`` for every case.
        """
        assert cli._parse_query_row_key("locomo|eval_v2|conv-26|q0") == ("conv-26", 0)
        assert cli._parse_query_row_key("locomo|eval_v2|conv-26|q12") == ("conv-26", 12)
        # Not a query row id → refused (no silent coercion).
        assert cli._parse_query_row_key("locomo|eval_v2|s|session_1|d1>d2") is None
        assert cli._parse_query_row_key("") is None
        assert cli._parse_query_row_key("locomo|eval_v2|s|x7") is None

    def test_build_q_emb_by_case_keys_are_case_ids(self):
        class _Row:
            def __init__(self, row_id):
                self.row_id = row_id

        class _Res:
            def __init__(self, vector):
                self.vector = vector

        out = cli._build_q_emb_by_case(
            query_rows=[_Row("locomo|eval_v2|conv-26|q0"),
                        _Row("locomo|eval_v2|conv-26|q3"),
                        _Row("not-a-query-row")],
            query_results=[_Res([1.0] * 1024), _Res([2.0] * 1024),
                           _Res([3.0] * 1024)],
        )
        assert set(out) == {"conv-26|0", "conv-26|3"}
        assert out["conv-26|3"][0] == 2.0

    def test_forwards_config_pg_and_objective_vectors(self, monkeypatch):
        captured = []

        def _fake_run_case(**kwargs):
            captured.append(kwargs)
            return {"case": kwargs.get("sample_id")}

        from eval.locomo_recall_v2 import runner as _runner
        monkeypatch.setattr(_runner, "run_case", _fake_run_case)

        lab_cfg = {
            "basePath": "/tmp/lab-base",
            "storage": {"pg": {"host": "127.0.0.1", "port": 55465,
                               "database": "g6ca2", "user": "lab",
                               "password": "PLACEHOLDER_LAB_PASS"}},
        }
        q_emb_by_case = {
            "s1|0": [0.5] * 1024,
            "s1|1": [0.25] * 1024,
        }
        cases = [
            {"sample_id": "s1", "query_idx": 0, "question": "Q0",
             "answer": "A", "gold_evidence_dia_ids": (),
             "gold_source_ids": ["src-0"], "unmapped_dia_ids": ["d-x"],
             "unresolved_evidence": ["d-1, d-2"], "category": "cat1"},
            {"sample_id": "s1", "query_idx": 1, "question": "Q1",
             "answer": "A", "gold_evidence_dia_ids": (),
             "gold_source_ids": ["src-1"], "unmapped_dia_ids": [],
             "unresolved_evidence": [], "category": "cat2"},
        ]
        sentinel_pg = object()
        cli._run_facade_pass(
            cases=cases, q_emb_by_case=q_emb_by_case,
            config=lab_cfg, pg=sentinel_pg, expected_dim=1024,
        )

        assert len(captured) == 2
        for call in captured:
            assert call["config"] is lab_cfg
            assert call["pg"] is sentinel_pg
            assert call["adapter_mode"] == "objective"
            assert call["expected_dim"] == 1024
            assert call["q_emb_by_case"] is q_emb_by_case
        # Per-case vectors are distinct and the evidence trio survives.
        assert captured[0]["unmapped_dia_ids"] == ("d-x",)
        assert captured[0]["unresolved_evidence"] == ("d-1, d-2",)
        assert captured[1]["unmapped_dia_ids"] == ()


class TestCredentialBoundary:
    """The borrowed provider credential stays in process memory only.

    The RUNTIME isolated config must carry the key (the provider call
    needs it in memory), so the boundary under test is not "the dict
    is keyless" but "no serialised projection ever carries it".
    """

    def test_embed_key_is_in_memory_only(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={
            "endpoint": "https://api.example.com/v1",
            "model": "BAAI/bge-m3",
            "api_key": "PLACEHOLDER_EMBED_KEY",
        })
        embed = cli.load_source_config_embed_section(str(cfg_path))
        # The exact credential keys are allowed in memory … (required:
        # the provider call cannot authenticate without them).
        assert embed["api_key"] == "PLACEHOLDER_EMBED_KEY"

        base = tmp_path / "base"
        base.mkdir()
        lab_cfg = cli.build_isolated_lab_config(
            base_path=str(base), dsn=GOOD_DSN,
            output_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
            embed_cfg=embed,
        )
        # … the runtime dict keeps it for the in-memory provider call …
        assert lab_cfg["storage"]["embed"]["api_key"] == "PLACEHOLDER_EMBED_KEY"

        # … but every serialised projection strips it:
        # 1) the fingerprint projection used for artifacts,
        stripped = cli._strip_endpoint_from_profile(lab_cfg["storage"]["embed"])
        assert "PLACEHOLDER_EMBED_KEY" not in json.dumps(stripped)
        assert "endpoint" not in stripped
        assert stripped["model"] == "BAAI/bge-m3"
        # 2) the generic payload redactor, and
        scrubbed = cli.redact_secrets_in_payload(lab_cfg)
        assert "PLACEHOLDER_EMBED_KEY" not in json.dumps(scrubbed)
        assert "PLACEHOLDER_LAB_PASS" not in json.dumps(scrubbed)
        # 3) the dry-run summary shape (keys only, never values).
        summary_keys = cli.StructuralResult(
            dataset_sha256="0" * 64, source_config_sha256="0" * 64,
            sample_count=1, session_count=1, message_count=1,
            qa_pair_count=1, eval_row_count=1, manifest={},
            manifest_path="x", lab_config=lab_cfg, base_path=str(base),
        ).to_dict()
        assert "PLACEHOLDER_EMBED_KEY" not in json.dumps(summary_keys)

    def test_exit_codes_are_distinct(self):
        codes = {
            cli.EXIT_OK,
            cli.EXIT_INPUT_ERROR,
            cli.EXIT_INTEGRATION_GAP,
            cli.EXIT_RUNTIME_ERROR,
        }
        assert len(codes) == 4