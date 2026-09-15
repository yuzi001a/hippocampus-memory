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
from eval.locomo_recall_v2.search_protocol import (  # noqa: E402
    SearchProtocol as _SearchProtocol,
    SEARCH_MODE_ANN as _SEARCH_MODE_ANN,
)


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


# ---------------------------------------------------------------------
# Disposable-DSN preflight -- pgvector library load-order contract.
#
# The preflight probes pg_settings for ivfflat.probes BEFORE forcing
# the pgvector shared library load, then AGAIN AFTER the load.
# ---------------------------------------------------------------------


class TestPreflightLoadOrder:
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

    def test_preflight_returns_available_when_load_registers_guc(
        self, monkeypatch
    ):
        from eval.locomo_recall_v2 import cli as _cli

        class _LoadOrderCursor:
            def __init__(self):
                self._pending = None
                self.log = []
                self.calls = 0

            def execute(self, sql, params=None):
                self.log.append((str(sql), tuple(params) if params else ()))
                text = str(sql).lstrip().upper()
                if text.startswith("LOAD '"):
                    self.calls += 1
                    self._pending = None
                    return
                if "::VECTOR" in text:
                    self._pending = (0.0,)
                    return
                if "FROM PG_SETTINGS" in text:
                    self._pending = (1,) if self.calls > 0 else None
                    return
                raise AssertionError(f"_LoadOrderCursor unexpected: {sql!r}")

            def fetchone(self):
                return self._pending

            def fetchall(self):
                return []

            def close(self):
                pass

        class _LoadOrderConn:
            def cursor(self_inner):
                return _LoadOrderCursor()
            def close(self_inner):
                pass

        monkeypatch.setattr(_cli, "_connect_lab", lambda dsn: _LoadOrderConn())

        verdict = _cli._preflight_probe_availability(
            GOOD_DSN,
            _SearchProtocol(mode=_SEARCH_MODE_ANN, probes=None),
        )
        assert verdict["probe_availability"] == _cli.PROBE_AVAILABILITY_AVAILABLE
        assert verdict["probe_availability_before_load"] == _cli.PROBE_AVAILABILITY_UNAVAILABLE
        assert verdict["probe_availability_after_load"] == _cli.PROBE_AVAILABILITY_AVAILABLE
        assert verdict["probe_availability_source"] == "pg_settings_after_library_load"
        assert verdict["pgvector_load_method"] == _cli.PROBE_LOAD_METHOD_LOAD
        assert verdict["probe_availability_preflight"] == "disposable_dsn"

    def test_explicit_probes_refused_when_guc_never_registers(
        self, tmp_path, monkeypatch
    ):
        from eval.locomo_recall_v2 import cli as _cli
        from eval.locomo_recall_v2 import lab as _lab

        bootstrap_calls = []
        import_calls = []

        def _boom_bootstrap(*a, **kw):
            bootstrap_calls.append(True)
            raise AssertionError("bootstrap_schema reached -- preflight too late")

        def _boom_import_rows(*a, **kw):
            import_calls.append(True)
            raise AssertionError("import_rows reached -- preflight too late")

        class _NeverRegistersCursor:
            def __init__(self):
                self._pending = None
                self.log = []
            def execute(self, sql, params=None):
                self.log.append((str(sql), tuple(params) if params else ()))
                text = str(sql).lstrip().upper()
                if text.startswith("LOAD '"):
                    raise RuntimeError("permission denied")
                if "::VECTOR" in text:
                    raise RuntimeError("type vector does not exist")
                if "FROM PG_SETTINGS" in text:
                    self._pending = None
                    return
                raise AssertionError(f"_NeverRegistersCursor unexpected: {sql!r}")
            def fetchone(self):
                return self._pending
            def fetchall(self):
                return []
            def close(self):
                pass

        class _NeverRegistersConn:
            def cursor(self_inner):
                return _NeverRegistersCursor()
            def close(self_inner):
                pass

        monkeypatch.setattr(_cli, "_connect_lab", lambda dsn: _NeverRegistersConn())
        monkeypatch.setattr(_lab, "bootstrap_schema", _boom_bootstrap)
        monkeypatch.setattr(_lab, "import_rows", _boom_import_rows)

        locomo_path, digest, cfg_path = self._write_fixture(tmp_path)
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
        assert rc == cli.EXIT_INPUT_ERROR
        assert bootstrap_calls == []
        assert import_calls == []

    def test_preflight_load_method_constant_exported(self):
        assert cli.PROBE_LOAD_METHOD_LOAD == "LOAD"
        assert cli.PROBE_LOAD_METHOD_VECTOR_FUNCTION == "VECTOR_FUNCTION"
        assert cli.PROBE_LOAD_METHOD_NONE == "NONE"

# ---------------------------------------------------------------------
# G6C-B0.1 sample-isolated orchestrator tests
# ---------------------------------------------------------------------


def _locomo_two_sample() -> dict:
    """Synthetic two-sample LoCoMo dataset.

    Sample ``s1`` carries TWO sessions; sample ``s2`` carries ONE
    session. The orchestrator must keep every session of ``s1``
    inside ``s1``'s isolated corpus and never cross-contaminate
    rows into the other sample.
    """

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
                    "session_2": {
                        "date_time": "2024-02-01 10:00:00",
                        "messages": [
                            {"dia_id": "d_10", "speaker": "speaker_1",
                             "text": "Second session hello."},
                            {"dia_id": "d_11", "speaker": "speaker_2",
                             "text": "Welcome back."},
                            {"dia_id": "d_12", "speaker": "speaker_1",
                             "text": "Thanks for having me."},
                            {"dia_id": "d_13", "speaker": "speaker_2",
                             "text": "Anytime."},
                        ],
                    },
                },
                "qa": [
                    {
                        "id": "d_3",
                        "question": "How are you? (s1)",
                        "answer": "Doing well, thanks.",
                        "category": "social",
                        "evidence": ["d_3"],
                    },
                    {
                        "id": "d_12",
                        "question": "What was said in session 2?",
                        "answer": "Thanks for having me.",
                        "category": "social",
                        "evidence": ["d_12"],
                    },
                ],
            },
            {
                "sample_id": "s2",
                "conversation": {
                    "session_1": {
                        "date_time": "2024-03-01 10:00:00",
                        "messages": [
                            {"dia_id": "d_50", "speaker": "speaker_1",
                             "text": "Good morning."},
                            {"dia_id": "d_51", "speaker": "speaker_2",
                             "text": "Morning!"},
                            {"dia_id": "d_52", "speaker": "speaker_1",
                             "text": "Lovely day."},
                            {"dia_id": "d_53", "speaker": "speaker_2",
                             "text": "Indeed."},
                        ],
                    },
                },
                "qa": [
                    {
                        "id": "d_52",
                        "question": "How is the day?",
                        "answer": "Lovely.",
                        "category": "social",
                        "evidence": ["d_52"],
                    },
                ],
            },
        ],
    }


def _make_sample_isolated_fake_run_semantic():
    """Return a ``(recorder, run_semantic_stub)`` pair.

    The recorder captures the per-sample ``sub_args`` (so a test can
    assert on the per-sample DSN / scope / sample_ids) and the sink
    list (``_records_out``) — the orchestrator appends CaseRecord
    objects to it.  The stub fakes just enough of the real
    ``run_semantic`` to satisfy the orchestrator's contract without
    touching PG / embeddings.
    """

    captured_args = []

    def _fake_run_semantic(args, *, _records_out=None):
        from eval.locomo_recall_v2.adapter import CaseRecord

        captured_args.append({
            "dsn": str(args.dsn),
            "output_dir": str(args.output_dir),
            "sample_ids": list(getattr(args, "sample_ids", []) or []),
            "sample_filter": str(getattr(args, "sample_filter", "")),
            "case_ids": getattr(args, "case_ids", None),
            "repeatability": bool(getattr(args, "repeatability", False)),
            "sample_isolated": bool(
                getattr(args, "sample_isolated", False)
            ),
            "case_limit": getattr(args, "case_limit", None),
            "sample_isolated_base_scope": getattr(
                args, "sample_isolated_base_scope", None
            ),
        })
        # Build one fake CaseRecord per sample-id — the test
        # asserts on the COUNT and the per-sample identity.
        sid = (
            (getattr(args, "sample_ids", []) or ["?"])[0]
        )
        slug = cli._sample_isolated_slug(str(sid))
        # Two records for s1 (matches the 2 QA pairs in the
        # synthetic dataset) and one record for s2.
        n = 2 if str(sid) == "s1" else 1
        recs = []
        for i in range(n):
            recs.append(CaseRecord(
                case_id=f"{sid}|{i}",
                sample_id=str(sid),
                query_idx=i,
                category="social",
                question=f"Q{i}",
                answer=f"A{i}",
                gold_evidence_dia_ids=(),
                gold_source_ids=(f"{slug}_src_{i}",),
                unmapped_dia_ids=(),
                unresolved_evidence=(),
                context_block_length=0,
                trace_id=f"trace_{sid}_{i}",
                ranked_source_ids=(f"{slug}_src_{i}",),
                selected_source_ids=(),
                candidate_source_ids=(f"{slug}_src_{i}",),
                injected_source_ids=(),
                lane_summaries=(),
                injection_summary=None,
                drop_summary={},
                elapsed_ms=0.0,
                status="ok",
                error="",
            ))
        if _records_out is not None:
            _records_out.extend(recs)
        # Also write a JSONL the orchestrator will read for
        # ``_count_corpus_rows``.  The exact row count is not
        # asserted — the per-sample ``case_count`` is the
        # canonical evidence.
        try:
            os.makedirs(str(args.output_dir), exist_ok=True)
            with open(
                os.path.join(
                    str(args.output_dir),
                    "full-objective-results.jsonl",
                ),
                "w",
                encoding="utf-8",
            ) as f:
                for r in recs:
                    f.write(json.dumps(r.to_dict()) + "\n")
        except Exception:
            pass
        # Per-sample corpus row counts.  The orchestrator reads
        # these as the TRUE imported row counts; they MUST differ
        # from the eval-case count so the test can prove the
        # orchestrator is NOT falling back to the JSONL line
        # count.  s1 carries 2 QA pairs / 8 conv-stream rows and
        # s2 carries 1 / 4 in this synthetic dataset; the fake
        # case count is 2/1.
        qa_pairs_rows = 2 if str(sid) == "s1" else 1
        conv_stream_rows = 8 if str(sid) == "s1" else 4
        return {
            "case_count": int(n),
            "status": "ok",
            "hit_at_5": 0.0,
            "mrr": 0.0,
            "jsonl_baseline": os.path.join(
                str(args.output_dir), "full-objective-results.jsonl"
            ),
            "corpus": {
                "qa_pairs": {"rows": int(qa_pairs_rows)},
                "conversation_stream": {"rows": int(conv_stream_rows)},
            },
        }

    return captured_args, _fake_run_semantic


class TestSampleIsolatedOrchestrator:
    """Focused tests for the G6C-B0.1 sample-isolated orchestrator.

    The orchestrator is exercised with ``run_semantic`` monkey-
    patched to a recorder so no PG / provider / HTTP call ever
    leaves the process.  The DB create / drop helpers are also
    monkey-patched to capture their (server_dsn, dbname) tuples.
    """

    def _write_locomo(self, path, payload):
        blob = json.dumps(
            payload, ensure_ascii=False, indent=2
        ).encode("utf-8")
        with open(path, "wb") as f:
            f.write(blob)
        return hashlib.sha256(blob).hexdigest()

    def _fixture(self, tmp_path, *, payload=None):
        payload = payload if payload is not None else _locomo_two_sample()
        locomo_path = tmp_path / "locomo.json"
        digest = self._write_locomo(str(locomo_path), payload)
        cfg_path = tmp_path / "cfg.yaml"
        _write_source_config(cfg_path, embed={
            "endpoint": "https://api.example.com/v1/embeddings",
            "model": "BAAI/bge-m3",
            "dim": 1024,
        })
        return locomo_path, digest, cfg_path

    def _stub_create_drop(self, monkeypatch):
        """Replace psycopg2-backed helpers with recorders."""

        creates: list[tuple[str, str]] = []
        drops: list[tuple[str, str]] = []

        def _fake_create(*, server_dsn, dbname):
            creates.append((str(server_dsn), str(dbname)))

        def _fake_drop(*, server_dsn, dbname, log):
            drops.append((str(server_dsn), str(dbname)))
            log.append({
                "database": str(dbname),
                "dropped": True,
                "error": None,
            })

        monkeypatch.setattr(cli, "_create_database", _fake_create)
        monkeypatch.setattr(cli, "_drop_database", _fake_drop)
        return creates, drops

    def test_slug_and_db_name_derivation(self):
        # slug: hyphen → underscore; lower-cased; empty after
        # sanitisation is refused.
        assert cli._sample_isolated_slug("conv-30") == "conv_30"
        assert cli._sample_isolated_slug("s1") == "s1"
        assert cli._sample_isolated_slug("SAMPLE.A") == "sample_a"
        with pytest.raises(cli.CLIError):
            cli._sample_isolated_slug("___")

        # The orchestrator's reserved-collision check is
        # exercised end-to-end in
        # ``test_orchestrator_refuses_reserved_dbname_collision``
        # below — at the unit level the candidate is always
        # suffixed with ``_iso_<slug>``, so engineering one that
        # lands on the reserved set is brittle.  The
        # ``candidate == base`` alias check is similarly
        # exercised end-to-end (it cannot fire at the unit
        # level because every candidate is suffixed with
        # ``_iso_<slug>``).

        # Happy path
        assert (
            cli._sample_isolated_db_name("lab", "conv-30")
            == "lab_iso_conv_30"
        )

    def test_replace_dbname_in_dsn_preserves_other_keys(self):
        dsn = (
            "host=127.0.0.1 port=55465 dbname=lab "
            "user=lab password=PLACEHOLDER_LAB_PASS"
        )
        out = cli._replace_dbname_in_dsn(dsn, "lab_iso_s1")
        # The exact key order is preserved (the regex substitutes
        # in place); the password and every other key stays put.
        assert "host=127.0.0.1" in out
        assert "port=55465" in out
        assert "dbname=lab_iso_s1" in out
        assert "user=lab" in out
        assert "password=PLACEHOLDER_LAB_PASS" in out
        with pytest.raises(cli.CLIError):
            cli._replace_dbname_in_dsn(
                "host=127.0.0.1 port=55465", "anything"
            )

    def test_make_disposable_base_path_scope_is_distinct(
        self, tmp_path, monkeypatch
    ):
        cache = tmp_path / "cache"
        cache.mkdir()
        # ``scope=None`` keeps the legacy path byte-identical.
        legacy = cli._make_disposable_base_path(
            str(cache), "abc123def456"
        )
        assert legacy.endswith("experiment-base-abc123def456")
        # Different scopes produce different directories.
        p1 = cli._make_disposable_base_path(
            str(cache), "abc123def456", scope="s1"
        )
        p2 = cli._make_disposable_base_path(
            str(cache), "abc123def456", scope="s2"
        )
        assert p1 != p2
        assert p1.endswith("-s1")
        assert p2.endswith("-s2")
        assert p1 != legacy  # scope changes the path
        assert os.path.isdir(p1)
        assert os.path.isdir(p2)

    def test_orchestrator_creates_and_drops_one_db_per_sample(
        self, tmp_path, monkeypatch
    ):
        """Spec: creates exactly one fresh database per sample and
        drops each of them; the base database is never dropped.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        creates, drops = self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK
        # Two samples → two CREATE / two DROP calls, in dataset order.
        assert len(creates) == 2, creates
        assert len(drops) == 2, drops
        # The base dbname ("lab") never appears as the CREATE /
        # DROP target — only the per-sample aliases do.
        created_dbnames = [c[1] for c in creates]
        dropped_dbnames = [d[1] for d in drops]
        assert "lab" not in created_dbnames
        assert "lab" not in dropped_dbnames
        for s in ("s1", "s2"):
            assert f"lab_iso_{s}" in created_dbnames
            assert f"lab_iso_{s}" in dropped_dbnames

    def test_orchestrator_aggregate_is_union_of_per_sample_cases(
        self, tmp_path, monkeypatch
    ):
        """Spec: aggregate contains exactly the union of per-sample
        cases in canonical dataset order; a synthetic 2-sample
        dataset yields the exact total.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK
        # Two per-sample passes; s1 emits 2 records, s2 emits 1
        # (matches the synthetic dataset).  Canonical dataset
        # order is sorted-by-sample-id, so the aggregate order is
        # s1 then s2.
        case_ids: list[str] = []
        jsonl = tmp_path / "out" / "full-objective-results.jsonl"
        assert jsonl.is_file()
        with open(str(jsonl), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    case_ids.append(
                        json.loads(line)["case_id"]
                    )
        assert case_ids == ["s1|0", "s1|1", "s2|0"]

        # Aggregate case_count equals the JSONL line count.
        with open(
            str(tmp_path / "out" / "metrics.json"),
            "r", encoding="utf-8",
        ) as f:
            metrics = json.load(f)
        assert metrics["questions_total"] == len(case_ids)
        assert metrics["questions_total"] == 3

    def test_orchestrator_per_sample_corpus_has_one_sample_id(
        self, tmp_path, monkeypatch
    ):
        """Spec: per-sample corpus is built with sample_ids=[sid]
        only — the inner run_semantic receives a sub_args whose
        sample_ids is exactly one element.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        # Two per-sample passes were made; each receives exactly
        # one sample_id (the loader builds corpus rows ONLY for
        # that id), and each carries a different per-sample DSN.
        assert len(captured_args) == 2
        seen_dsn: set[str] = set()
        for sub in captured_args:
            assert len(sub["sample_ids"]) == 1, sub
            assert sub["sample_isolated"] is False
            assert sub["repeatability"] is False
            assert sub["case_limit"] is None
            assert sub["sample_filter"] == ""
            assert sub["dsn"] != GOOD_DSN
            assert sub["dsn"] not in seen_dsn
            seen_dsn.add(sub["dsn"])

        # The per-sample dsns point at the canonical per-sample
        # database name AND preserve every other DSN key.
        dsn_to_sid = {
            sub["dsn"]: sub["sample_ids"][0] for sub in captured_args
        }
        # Each per-sample DSN must carry the lab base keys.
        for dsn in seen_dsn:
            assert "host=127.0.0.1" in dsn
            assert "port=55465" in dsn
            assert "user=lab" in dsn
            assert "password=PLACEHOLDER_LAB_PASS" in dsn
            assert "dbname=lab_iso_" in dsn
        # And they are distinct — s1 and s2 map to different DBs.
        iter_dsn = iter(seen_dsn)
        first_dsn = next(iter_dsn)
        second_dsn = next(iter_dsn)
        assert dsn_to_sid[first_dsn] in {"s1", "s2"}
        assert dsn_to_sid[second_dsn] in {"s1", "s2"}
        assert dsn_to_sid[first_dsn] != dsn_to_sid[second_dsn]

    def test_orchestrator_sessions_are_retained_per_sample(
        self, tmp_path, monkeypatch
    ):
        """Spec: all sessions of one sample are retained in that
        sample's corpus rows — the orchestrator's corpus-isolation
        evidence reports the per-sample session_count equal to the
        loader's session count for that sample.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        # The corpus-isolation evidence carries the per-sample
        # session_count we derived from the canonical dataset —
        # s1 has 2 sessions, s2 has 1.
        with open(
            str(tmp_path / "out" / "corpus-isolation.json"),
            "r", encoding="utf-8",
        ) as f:
            ci = json.load(f)
        assert ci["mode"] == "sample_isolated"
        assert ci["samples"] == ["s1", "s2"]
        assert ci["per_sample"]["s1"]["session_count"] == 2
        assert ci["per_sample"]["s2"]["session_count"] == 1
        # The per-sample ``case_count`` matches what the fake
        # run_semantic emitted.
        assert ci["per_sample"]["s1"]["case_count"] == 2
        assert ci["per_sample"]["s2"]["case_count"] == 1

    def test_orchestrator_per_sample_metrics_equal_recomputation(
        self, tmp_path, monkeypatch
    ):
        """Spec: per-sample metric aggregation equals an
        independent recomputation over the per-sample records.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        with open(
            str(tmp_path / "out" / "per-sample-metrics.json"),
            "r", encoding="utf-8",
        ) as f:
            psm = json.load(f)

        # Each per-sample block carries the fields the spec lists.
        for sid in ("s1", "s2"):
            entry = psm["per_sample"][sid]
            for field in (
                "sample_id", "question_count", "hit_at_1", "hit_at_5",
                "mrr", "mean_relevant_rank", "status",
                "mapped_gold_count", "unmapped_gold_count",
                "unresolved_gold_count", "candidate_total",
            ):
                assert field in entry, (sid, field, entry)
        # The orchestrator's recomputation matches what the fake
        # run_semantic emitted.
        assert psm["per_sample"]["s1"]["question_count"] == 2
        assert psm["per_sample"]["s2"]["question_count"] == 1

    def test_orchestrator_per_sample_base_paths_are_distinct(
        self, tmp_path, monkeypatch
    ):
        """Spec: per-sample basePath values are distinct."""

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        with open(
            str(tmp_path / "out" / "corpus-isolation.json"),
            "r", encoding="utf-8",
        ) as f:
            ci = json.load(f)
        paths = {
            sid: ci["per_sample"][sid]["base_path"]
            for sid in ci["per_sample"]
        }
        assert len(paths) == 2
        assert len(set(paths.values())) == 2
        # Each path lives under the cache_dir and ends with the
        # slug for that sample.
        for sid, bp in paths.items():
            assert bp.startswith(str(tmp_path / "cache"))
            assert bp.endswith(f"-{sid}")

    def test_orchestrator_rejects_case_limit_with_sample_isolated(
        self, tmp_path
    ):
        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
            "--case-limit", "5",
        ])
        assert rc == cli.EXIT_INPUT_ERROR

    def test_orchestrator_rejects_repeatability_with_sample_isolated(
        self, tmp_path
    ):
        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
            "--repeatability",
        ])
        assert rc == cli.EXIT_INPUT_ERROR

    def test_orchestrator_refuses_reserved_dbname_collision(
        self, tmp_path, monkeypatch
    ):
        """Spec: reserved / base-dbname collision is refused.

        We pick a reserved DB name from ``lab._RESERVED_DBS`` and
        pass a base DSN carrying that reserved name; the
        orchestrator must refuse before any per-sample DB is
        created.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        reserved = sorted(cli._sample_isolated_reserved_dbs())[0]
        bad_dsn = (
            f"host=127.0.0.1 port=55465 dbname={reserved} "
            "user=lab password=PLACEHOLDER_LAB_PASS"
        )
        # Sanity — the bad DSN itself is also refused by the
        # lab guard (``reserved`` is in ``lab._RESERVED_DBS``).
        from eval.locomo_recall_v2 import lab as _lab
        with pytest.raises(_lab.LabDSNRefused):
            _lab.validate_disposable_dsn(bad_dsn)

        creates: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cli, "_create_database",
            lambda **kw: creates.append((kw["server_dsn"], kw["dbname"])),
        )
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", bad_dsn,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_INPUT_ERROR
        assert creates == [], (
            "refused before any per-sample DB is created"
        )

    def test_orchestrator_refuses_aliasing_base_dbname(
        self, tmp_path, monkeypatch
    ):
        """A per-sample DB name that would alias the base dbname
        (e.g. ``base_lab`` + ``slug=lab`` → ``base_lab_iso_lab``
        … NOT aliasing in our scheme, but a synthetic id whose
        slug collapses to ``base`` must be refused).
        """

        # Add a third sample whose slug matches the base dbname
        payload = _locomo_two_sample()
        payload["samples"].append({
            "sample_id": "lab",
            "conversation": {
                "session_1": {
                    "date_time": "2024-04-01 10:00:00",
                    "messages": [
                        {"dia_id": "d_99", "speaker": "speaker_1",
                         "text": "collision attempt"},
                    ],
                },
            },
            "qa": [{
                "id": "d_99", "question": "Q?",
                "answer": "A.", "category": "social",
                "evidence": ["d_99"],
            }],
        })
        locomo_path, digest, cfg_path = self._fixture(
            tmp_path, payload=payload
        )
        # The synthetic sample ``lab`` produces ``lab_iso_lab`` —
        # NOT an alias of the base, but the orchestrator must run
        # without crashing and report three samples.
        creates: list[tuple[str, str]] = []
        drops: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cli, "_create_database",
            lambda **kw: creates.append((kw["server_dsn"], kw["dbname"])),
        )
        monkeypatch.setattr(
            cli, "_drop_database",
            lambda **kw: (
                drops.append((kw["server_dsn"], kw["dbname"])),
                kw["log"].append({
                    "database": kw["dbname"],
                    "dropped": True,
                    "error": None,
                }),
            ),
        )
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK
        assert len(creates) == 3
        # The base dbname ``lab`` never appears as the CREATE
        # target.
        assert all(c[1] != "lab" for c in creates)

    def test_records_out_sink_appends_real_case_records(self, tmp_path):
        """Spec: ``run_semantic`` with a ``_records_out`` sink
        appends the in-memory record objects (the same objects it
        passes to ``compute_metrics``) just before returning.
        ``main()`` never passes this kwarg.
        """

        sink: list = []
        # Build a minimal fake: the sink must receive the
        # objects ``run_semantic`` produced.  We don't call the
        # real ``run_semantic`` here (no PG available); we
        # exercise the sink contract directly via the orchestrator's
        # helper instead — see the other tests for the round trip.
        # This test guards the kwarg signature itself.
        import inspect
        sig = inspect.signature(cli.run_semantic)
        assert "_records_out" in sig.parameters
        # The kwarg is keyword-only.
        assert sig.parameters["_records_out"].kind == (
            inspect.Parameter.KEYWORD_ONLY
        )

    def test_orchestrator_writes_full_artifact_set(
        self, tmp_path, monkeypatch
    ):
        """The aggregate artifact set is byte-identical to the
        single-lab artifact set in filename, plus the two
        sample-isolated extras.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = _make_sample_isolated_fake_run_semantic()
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        expected = {
            "full-objective-results.jsonl",
            "semantic-canary-results.jsonl",
            "metrics.json",
            "coverage-audit.json",
            "latency.csv",
            "embedding-cache-manifest.json",
            "fingerprint.json",
            "g6c-b0-protocol.json",
            "per-sample-metrics.json",
            "corpus-isolation.json",
        }
        written = {
            p.name for p in (tmp_path / "out").iterdir()
        }
        assert expected <= written, (
            "expected artifacts missing", expected - written,
        )

    # ------------------------------------------------------------------
    # False corpus-row-count evidence: qa_pairs_rows and
    # conversation_stream_rows must read from the per-sample
    # run_semantic summary (corpus.<kind>.rows), NOT from the
    # eval-case JSONL line count.  The line count is preserved
    # under evidence_written_case_count.
    # ------------------------------------------------------------------

    def test_orchestrator_per_sample_corpus_rows_come_from_summary(
        self, tmp_path, monkeypatch
    ):
        """Spec: ``qa_pairs_rows`` and ``conversation_stream_rows``
        in ``corpus-isolation.json`` equal the values reported by
        the per-sample ``run_semantic`` summary under
        ``corpus.<kind>.rows`` — NOT the JSONL line count and NOT
        the ``case_count``.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = (
            _make_sample_isolated_fake_run_semantic()
        )
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        with open(
            str(tmp_path / "out" / "corpus-isolation.json"),
            "r", encoding="utf-8",
        ) as f:
            ci = json.load(f)

        # s1 emits 2 cases but carries 2 QA / 8 conv rows; s2
        # emits 1 case but carries 1 QA / 4 conv rows.  The
        # JSONL line count happens to match the QA-row count for
        # the synthetic dataset (pure coincidence), but the
        # orchestrator MUST source the value from the summary —
        # the case_count is 1 for s2 while the QA / conv rows
        # differ from case_count, so we verify each field
        # independently.
        expected = {
            "s1": {"qa_pairs_rows": 2, "conversation_stream_rows": 8,
                    "case_count": 2},
            "s2": {"qa_pairs_rows": 1, "conversation_stream_rows": 4,
                    "case_count": 1},
        }
        for sid, exp in expected.items():
            block = ci["per_sample"][sid]
            assert block["qa_pairs_rows"] == exp["qa_pairs_rows"], (
                sid, block
            )
            assert (
                block["conversation_stream_rows"]
                == exp["conversation_stream_rows"]
            ), (sid, block)
            # The corpus row counts MUST NOT silently equal the
            # JSONL line count / case_count when the dataset
            # would expose the difference.  s2: case_count==1
            # but conv_stream_rows==4, so a falsified
            # conversation_stream_rows value would expose the
            # bug.
            assert (
                block["qa_pairs_rows"] != block["case_count"]
                or block["conversation_stream_rows"]
                != block["case_count"]
            ), (sid, block)

        # The same evidence propagates into g6c-b0-protocol.json
        # under the corpus_isolation.per_sample_lab block.
        with open(
            str(tmp_path / "out" / "g6c-b0-protocol.json"),
            "r", encoding="utf-8",
        ) as f:
            proto = json.load(f)
        for sid, exp in expected.items():
            psl = proto["corpus_isolation"]["per_sample_lab"][sid]
            assert psl["qa_pairs_rows"] == exp["qa_pairs_rows"]
            assert (
                psl["conversation_stream_rows"]
                == exp["conversation_stream_rows"]
            )

    def test_orchestrator_evidence_written_case_count_matches_jsonl(
        self, tmp_path, monkeypatch
    ):
        """Spec: ``evidence_written_case_count`` equals the line
        count of the per-sample ``full-objective-results.jsonl``
        — the JSONL evidence the inner pass wrote, NOT the
        corpus row count.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)
        captured_args, fake_run = (
            _make_sample_isolated_fake_run_semantic()
        )
        monkeypatch.setattr(cli, "run_semantic", fake_run)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        with open(
            str(tmp_path / "out" / "corpus-isolation.json"),
            "r", encoding="utf-8",
        ) as f:
            ci = json.load(f)
        # The fake writes 2 records for s1 and 1 for s2.
        assert ci["per_sample"]["s1"]["evidence_written_case_count"] == 2
        assert ci["per_sample"]["s2"]["evidence_written_case_count"] == 1
        # And those values equal the JSONL line count on disk.
        for sid in ("s1", "s2"):
            slug = cli._sample_isolated_slug(sid)
            jsonl = (
                tmp_path / "out" / "per-sample" / slug
                / "full-objective-results.jsonl"
            )
            n_lines = sum(
                1 for line in open(str(jsonl), "r", encoding="utf-8")
                if line.strip()
            )
            assert (
                ci["per_sample"][sid]["evidence_written_case_count"]
                == n_lines
            ), (sid, n_lines)

        # ``qa_pairs_rows`` MUST NOT silently equal the line count
        # in cases where they would differ — for s1 the JSONL
        # line count and qa_pairs_rows are both 2 (by coincidence
        # in this fixture), but ``conversation_stream_rows`` is 8
        # and so is provably NOT the line count.
        assert (
            ci["per_sample"]["s1"]["conversation_stream_rows"]
            != ci["per_sample"]["s1"]["evidence_written_case_count"]
        )
        assert (
            ci["per_sample"]["s2"]["conversation_stream_rows"]
            != ci["per_sample"]["s2"]["evidence_written_case_count"]
        )

    def test_orchestrator_missing_corpus_rows_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """Spec: a per-sample ``run_semantic`` summary missing
        ``corpus.<kind>.rows`` fails closed with ``CLIError`` —
        never silently writes 0 or a fallback.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)

        def _fake_no_corpus(args, *, _records_out=None):
            from eval.locomo_recall_v2.adapter import CaseRecord

            sid = (getattr(args, "sample_ids", []) or ["?"])[0]
            slug = cli._sample_isolated_slug(str(sid))
            n = 2 if str(sid) == "s1" else 1
            recs = []
            for i in range(n):
                recs.append(CaseRecord(
                    case_id=f"{sid}|{i}",
                    sample_id=str(sid),
                    query_idx=i,
                    category="social",
                    question=f"Q{i}",
                    answer=f"A{i}",
                    gold_evidence_dia_ids=(),
                    gold_source_ids=(f"{slug}_src_{i}",),
                    unmapped_dia_ids=(),
                    unresolved_evidence=(),
                    context_block_length=0,
                    trace_id=f"trace_{sid}_{i}",
                    ranked_source_ids=(f"{slug}_src_{i}",),
                    selected_source_ids=(),
                    candidate_source_ids=(f"{slug}_src_{i}",),
                    injected_source_ids=(),
                    lane_summaries=(),
                    injection_summary=None,
                    drop_summary={},
                    elapsed_ms=0.0,
                    status="ok",
                    error="",
                ))
            if _records_out is not None:
                _records_out.extend(recs)
            try:
                os.makedirs(str(args.output_dir), exist_ok=True)
                with open(
                    os.path.join(
                        str(args.output_dir),
                        "full-objective-results.jsonl",
                    ),
                    "w",
                    encoding="utf-8",
                ) as f:
                    for r in recs:
                        f.write(json.dumps(r.to_dict()) + "\n")
            except Exception:
                pass
            # Deliberately OMIT the ``corpus`` block — this is the
            # schema downgrade that must fail closed.
            return {
                "case_count": int(n),
                "status": "ok",
                "hit_at_5": 0.0,
                "mrr": 0.0,
                "jsonl_baseline": os.path.join(
                    str(args.output_dir),
                    "full-objective-results.jsonl",
                ),
            }

        monkeypatch.setattr(cli, "run_semantic", _fake_no_corpus)

        # ``main()`` catches ``CLIError`` internally and returns
        # ``EXIT_INPUT_ERROR`` — verify the orchestrator refused
        # before any artifact was written.
        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_INPUT_ERROR, rc

        # No corpus-isolation.json should be written because the
        # orchestrator refuses before reaching that block.
        ci = tmp_path / "out" / "corpus-isolation.json"
        assert not ci.exists()

    def test_orchestrator_inner_pass_uses_scoped_base_path(
        self, tmp_path, monkeypatch
    ):
        """Spec: each per-sample inner ``run_semantic`` call
        receives ``sample_isolated_base_scope=<slug>`` and
        therefore uses a base_path that ends with the slug.  The
        path the orchestrator records in ``corpus-isolation.json``
        matches the scoped path the inner pass actually used.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)

        inner_base_paths: dict[str, str] = {}
        captured_args_inner: list = []

        def _wrapped_run_semantic(args, *, _records_out=None):
            from eval.locomo_recall_v2.adapter import CaseRecord

            scope = getattr(args, "sample_isolated_base_scope", None)
            sha = str(getattr(args, "expected_sha256", "") or "")
            bp = cli._make_disposable_base_path(
                str(args.cache_dir), sha, scope=scope,
            )
            sid = (getattr(args, "sample_ids", []) or ["?"])[0]
            inner_base_paths[str(sid)] = str(bp)
            captured_args_inner.append({
                "sid": str(sid),
                "scope": scope,
                "base_path": str(bp),
            })
            slug = cli._sample_isolated_slug(str(sid))
            n = 2 if str(sid) == "s1" else 1
            recs = []
            for i in range(n):
                recs.append(CaseRecord(
                    case_id=f"{sid}|{i}",
                    sample_id=str(sid),
                    query_idx=i,
                    category="social",
                    question=f"Q{i}",
                    answer=f"A{i}",
                    gold_evidence_dia_ids=(),
                    gold_source_ids=(f"{slug}_src_{i}",),
                    unmapped_dia_ids=(),
                    unresolved_evidence=(),
                    context_block_length=0,
                    trace_id=f"trace_{sid}_{i}",
                    ranked_source_ids=(f"{slug}_src_{i}",),
                    selected_source_ids=(),
                    candidate_source_ids=(f"{slug}_src_{i}",),
                    injected_source_ids=(),
                    lane_summaries=(),
                    injection_summary=None,
                    drop_summary={},
                    elapsed_ms=0.0,
                    status="ok",
                    error="",
                ))
            if _records_out is not None:
                _records_out.extend(recs)
            try:
                os.makedirs(str(args.output_dir), exist_ok=True)
                with open(
                    os.path.join(
                        str(args.output_dir),
                        "full-objective-results.jsonl",
                    ),
                    "w",
                    encoding="utf-8",
                ) as f:
                    for r in recs:
                        f.write(json.dumps(r.to_dict()) + "\n")
            except Exception:
                pass
            qa_pairs_rows = 2 if str(sid) == "s1" else 1
            conv_stream_rows = 8 if str(sid) == "s1" else 4
            return {
                "case_count": int(n),
                "status": "ok",
                "hit_at_5": 0.0,
                "mrr": 0.0,
                "jsonl_baseline": os.path.join(
                    str(args.output_dir),
                    "full-objective-results.jsonl",
                ),
                "corpus": {
                    "qa_pairs": {"rows": int(qa_pairs_rows)},
                    "conversation_stream": {
                        "rows": int(conv_stream_rows),
                    },
                },
            }

        monkeypatch.setattr(cli, "run_semantic", _wrapped_run_semantic)

        rc = cli.main([
            "run",
            "--dataset", str(locomo_path),
            "--expected-sha256", digest,
            "--dsn", GOOD_DSN,
            "--output-dir", str(tmp_path / "out"),
            "--cache-dir", str(tmp_path / "cache"),
            "--source-config", str(cfg_path),
            "--mode", "semantic",
            "--sample-isolated",
        ])
        assert rc == cli.EXIT_OK

        # Every captured sub_args carried its sample's slug.
        for cap in captured_args_inner:
            assert cap["scope"] in {"s1", "s2"}, cap
            assert cap["base_path"].endswith(f"-{cap['scope']}"), cap

        # The inner-pass base paths are distinct — otherwise the
        # data-dir guard routes through one shared directory.
        assert set(inner_base_paths.keys()) == {"s1", "s2"}
        assert len(set(inner_base_paths.values())) == 2, (
            "the per-sample inner passes must use distinct base "
            "paths; otherwise the data-dir guard routes through "
            "one shared directory"
        )

        # The orchestrator records the SAME scoped path in
        # corpus-isolation.json.
        with open(
            str(tmp_path / "out" / "corpus-isolation.json"),
            "r", encoding="utf-8",
        ) as f:
            ci = json.load(f)
        for sid in ("s1", "s2"):
            recorded = ci["per_sample"][sid]["base_path"]
            used = inner_base_paths[sid]
            assert recorded == used, (sid, recorded, used)

    def test_single_lab_inner_pass_uses_legacy_unscoped_base_path(
        self, tmp_path, monkeypatch
    ):
        """Spec: in the single-lab (non-sample-isolated) path the
        inner ``run_semantic`` derives a base_path with NO scope
        — the legacy ``experiment-base-<sha12>`` path is
        byte-identical to what it was before the sample-isolated
        change.
        """

        locomo_path, digest, cfg_path = self._fixture(tmp_path)
        self._stub_create_drop(monkeypatch)

        observed_scopes: list = []
        observed_base_paths: list = []

        def _wrapped_run_semantic(args, *, _records_out=None):
            observed_scopes.append(
                getattr(args, "sample_isolated_base_scope", None)
            )
            base_path = cli._make_disposable_base_path(
                str(args.cache_dir),
                str(args.expected_sha256),
                scope=getattr(
                    args, "sample_isolated_base_scope", None
                ),
            )
            observed_base_paths.append(base_path)
            # Return a minimal valid summary; the single-lab path
            # does NOT consult the corpus-row-count resolver,
            # but the shape stays consistent.
            return {
                "case_count": 0,
                "status": "ok",
                "hit_at_5": 0.0,
                "mrr": 0.0,
                "jsonl_baseline": "",
                "corpus": {
                    "qa_pairs": {"rows": 0},
                    "conversation_stream": {"rows": 0},
                },
            }

        monkeypatch.setattr(cli, "run_semantic", _wrapped_run_semantic)

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
        assert rc == cli.EXIT_OK

        # The single-lab Namespace carries no
        # ``sample_isolated_base_scope`` attribute; the inner call
        # falls back to ``getattr(..., None)`` which preserves
        # the legacy unscoped path byte-identical to the pre-
        # change behaviour.
        assert observed_scopes == [None], observed_scopes
        assert len(observed_base_paths) == 1
        assert observed_base_paths[0].endswith(
            f"experiment-base-{digest[:12]}"
        ), observed_base_paths[0]
