from __future__ import annotations

from io import StringIO

from v3core import first_run as fr


def test_pgvector_probe_retries_transient_socket_not_ready(monkeypatch):
    calls = {"n": 0}

    def run(cmd):
        calls["n"] += 1
        if calls["n"] < 3:
            return 2, "", "psql: could not connect: No such file or directory"
        return 0, "0.8.6\n", ""

    monkeypatch.setattr(fr.time, "sleep", lambda _seconds: None)
    out = StringIO()
    result = fr._probe_pgvector(
        run, "docker", "hippocampus-pg", database="v3embeddings_alpha",
        user="postgres", password="not-real", out=out,
    )
    assert result == "0.8.6"
    assert calls["n"] == 3
    assert "not-real" not in out.getvalue()


def test_pgvector_probe_does_not_retry_authentication_failure(monkeypatch):
    calls = {"n": 0}

    def run(cmd):
        calls["n"] += 1
        return 2, "", "password authentication failed for user postgres"

    monkeypatch.setattr(fr.time, "sleep", lambda _seconds: None)
    out = StringIO()
    result = fr._probe_pgvector(
        run, "docker", "hippocampus-pg", database="v3embeddings_alpha",
        user="postgres", password="not-real", out=out,
    )
    assert result is None
    assert calls["n"] == 1
    assert "not-real" not in out.getvalue()
