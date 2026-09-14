"""v3-core config CLI — thin control plane over ConfigControlService.

Surface:
    add_config_parser(subparsers)  -- argparse wiring
    run_config(args) -> int        -- dispatch + returncode

Design constraints (safety contract):
    * Never prints tracebacks; all exceptions are mapped to user-readable
      one-line messages + non-zero returncode.
    * Never echoes secret values; never logs secrets; never puts a secret
      into an exception message. ``set-key`` accepts the secret via stdin
      bytes or getpass — never via argv.
    * The CLI is a *thin* shell: every action delegates to
      ``ConfigControlService``. No provider imports happen until the user
      explicitly invokes ``config test``.
    * ``show`` only displays redacted/safe fields. Human output uses the
      service's safe URL formatter; ``--json`` uses the service's redacted
      JSON report.
    * Readiness reflects the local config shape only (CONFIGURED /
      NOT CONFIGURED / NOT TESTED) — it never claims live health.

Returncodes:
    0   success
    1   user/validation/CLI error (never printed as a traceback)
    2   internal/dispatch error
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any, Sequence


# ------------------------------------------------------------------ service


def _make_service(args: argparse.Namespace) -> Any:
    """Build a ConfigControlService from parsed CLI args.

    Imports lazily so that ``python -m v3core config --help`` doesn't pay
    for YAML import or service module load.
    """
    from .config_control import ConfigControlService

    profile = getattr(args, "profile", None)
    return ConfigControlService(profile=profile)


# ----------------------------------------------------------------- formatting


def _emit(line: str) -> None:
    """Single-line stdout write (no trailing newline normalisation tricks)."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _err(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


# ----------------------------------------------------- exception -> exitcode


def _handle_service_error(exc: Exception) -> int:
    """Map a service-side error to a one-line user message + exitcode.

    Never prints a traceback. Never echoes secret-shaped content. For
    ``ConfigValidationError`` we render the field-safe issue list; for
    other exceptions we fall back to the safe error helper and a generic
    prefix so the user knows the command failed without seeing internals.
    """
    from .config_control import ConfigValidationError

    if isinstance(exc, ConfigValidationError):
        for issue in exc.issues:
            _err(str(issue))
        return 1
    # Defensive: anything else is treated as a generic internal error.
    # We deliberately avoid ``str(exc)`` since third-party providers can
    # leak credentials in their exception text.
    try:
        from . import _safe_err  # type: ignore
        _safe_err(exc, 200)
    except Exception:
        pass
    _err(f"error: {type(exc).__name__} (no traceback printed)")
    return 2


# -------------------------------------------------------------------- show


def _run_show(args: argparse.Namespace, service: Any) -> int:
    """Render the config report (safe-formatter endpoint, never secrets).

    Delegates to ``service.show(redact=False, ...)`` so human and JSON
    outputs actually display endpoint / model values rather than just
    ``CONFIGURED``. The service still uses the safe URL formatter for
    endpoints and never echoes secrets (apiKey / api_key / password),
    so the on-disk state is exposed safely.

    After ``show`` returns we run offline ``service.validate()``:
    missing or malformed configuration surfaces as errors that still
    exit 1 even though the report was printed.
    """
    try:
        text = service.show(redact=False, as_json=bool(getattr(args, "json", False)))
    except Exception as exc:
        return _handle_service_error(exc)

    if getattr(args, "json", False):
        # JSON path: emit as-is (already redacted by the service).
        _emit(text)
    else:
        # Human-readable path. The service already prepends ``profile:`` /
        # ``path:`` lines, but our spec mandates uppercase ``PROFILE:`` and
        # ``CONFIG PATH:`` as the first two lines. We therefore re-emit those
        # two lines from service state and then forward the rest of the
        # report body.
        profile = getattr(service, "profile", "") or ""
        cfg_path = getattr(service, "path", None)
        cfg_path_str = str(cfg_path) if cfg_path is not None else ""
        _emit(f"PROFILE: {profile}")
        _emit(f"CONFIG PATH: {cfg_path_str}")

        # The service-emitted text starts with ``profile:`` / ``path:`` lines
        # and uses lowercase keys (``host``, ``port``, ... ``readiness``).
        # We forward everything *after* the first two header lines, prefixed
        # by a separator so the human report is unambiguous.
        lines = text.splitlines()
        # Drop leading "profile: ..." and "path:    ..." header lines from
        # the service text; the CLI owns those.
        rest: list[str] = []
        skipped = 0
        for ln in lines:
            if skipped < 2 and (ln.startswith("profile:") or ln.startswith("path:")):
                skipped += 1
                continue
            rest.append(ln)

        # Translate the conservative readiness token into the CLI's
        # CONFIGURED/NOT CONFIGURED/NOT TESTED vocabulary so we never confuse
        # "shape complete" with "network healthy". Known tokens (the ones
        # emitted by ``ConfigControlService._readiness`` /
        # ``_build_show_report``) get a precise phrase; anything else
        # is rendered explicitly as an unknown readiness state rather
        # than silently falling through to a known-looking phrase.
        def _readiness_line(rlines: list[str]) -> tuple[str, list[str]]:
            out: list[str] = []
            for rl in rlines:
                if rl.startswith("readiness:"):
                    tok = rl.split(":", 1)[1].strip()
                    if tok == "ok":
                        return "READY: CONFIG CONFIGURED (NOT TESTED)", out
                    if tok == "incomplete":
                        return "READY: CONFIG INCOMPLETE (NOT TESTED)", out
                    if tok == "missing_required":
                        return "READY: NOT CONFIGURED", out
                    if tok == "config_invalid":
                        return "READY: NOT CONFIGURED (config invalid)", out
                    if tok == "unparseable":
                        return "READY: NOT CONFIGURED (config unparseable)", out
                    # Unknown token — never silently treat as a known
                    # state. Quote the literal token so an operator can
                    # diagnose the source (service version drift /
                    # formatting corruption) without confusing it with
                    # a known CONFIGURED / NOT TESTED status.
                    return (
                        f"READY: NOT TESTED (unknown readiness state: {tok!r})",
                        out,
                    )
                out.append(rl)
            # No readiness line emitted at all — same explicit unknown
            # branch as an unrecognised token.
            return "READY: NOT TESTED (unknown readiness state)", out

        ready_line, rest = _readiness_line(rest)

        _emit("---")
        for ln in rest:
            _emit(ln)
        _emit(ready_line)

    # Offline validate so missing / malformed config surfaces as exit 1,
    # even though the safe report has already been emitted.
    try:
        issues = service.validate()
    except Exception as exc:
        return _handle_service_error(exc)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        for issue in errors:
            _emit(f"  validate: {issue}")
        return 1
    return 0


# ----------------------------------------------------------------- validate


def _run_validate(args: argparse.Namespace, service: Any) -> int:
    """Run offline validation only — never import providers / PG.

    Service-side ``validate()`` is structurally bound to ``read_data()``
    which only reads + parses the YAML. No network, no provider dispatch.
    """
    try:
        issues = service.validate()
    except Exception as exc:
        return _handle_service_error(exc)

    errors = [i for i in issues if i.severity == "error"]
    if errors:
        _emit("VALIDATION: FAIL")
        for issue in errors:
            _emit(f"  - {issue}")
        return 1

    warnings = [i for i in issues if i.severity == "warning"]
    _emit("VALIDATION: PASS")
    if warnings:
        _emit(f"warnings: {len(warnings)}")
        for issue in warnings:
            _emit(f"  - {issue}")
    return 0


# ---------------------------------------------------------------- set-provider


def _run_set_provider(args: argparse.Namespace, service: Any) -> int:
    """Set endpoint / model on one provider target.

    The CLI passes the user's ``--endpoint`` verbatim to the service. For
    the ``llm`` target we map ``--endpoint`` to the runtime field
    ``base_url`` — we never invent a new endpoint field; the service is
    the authority on where that value lands on disk.
    """
    target = getattr(args, "target", None)
    endpoint = getattr(args, "endpoint", None)
    model = getattr(args, "model", None)

    if not target:
        _err("error: target is required (embedding|rerank|llm)")
        return 1
    if endpoint is None and model is None:
        _err("error: at least one of --endpoint or --model is required")
        return 1

    try:
        service.set_provider(target=target, endpoint=endpoint, model=model)
    except Exception as exc:
        return _handle_service_error(exc)

    # Success line: report field path + validation PASS, never echo the
    # new value (it could be a sensitive URL).
    field_bits: list[str] = []
    if endpoint is not None:
        if target == "llm":
            field_bits.append("llm.base_url")
        elif target == "embedding":
            field_bits.append("storage.embed.endpoint")
        elif target == "rerank":
            field_bits.append("storage.rerank.endpoint")
        else:
            field_bits.append(f"{target}.endpoint")
    if model is not None:
        if target == "llm":
            field_bits.append("llm.model")
        elif target == "embedding":
            field_bits.append("storage.embed.model")
        elif target == "rerank":
            field_bits.append("storage.rerank.model")
        else:
            field_bits.append(f"{target}.model")
    _emit("SET-PROVIDER: PASS")
    _emit(f"  fields: {', '.join(field_bits)}")
    issues = service.validate()
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        _emit("VALIDATION: FAIL")
        for issue in errors:
            _emit(f"  - {issue}")
        return 1
    _emit("VALIDATION: PASS")
    return 0


# ------------------------------------------------------------------ set-key


def _read_secret_from_stdin() -> str:
    """Read the secret from stdin and normalize.

    In production, ``sys.stdin`` is a ``TextIOWrapper`` whose
    ``buffer`` attribute is the underlying ``FileIO`` and we call
    ``.read()`` on the raw bytes so CRLF / LF handling matches the
    service contract: one trailing newline is stripped, embedded
    control characters and empty input are rejected by
    ``normalize_secret_input``.

    Tests inject a plain ``io.StringIO`` whose ``buffer`` is missing;
    in that case we fall back to ``sys.stdin.read()`` (text). Either
    path decodes UTF-8 *strictly* so BOM / encoding errors surface as
    ordinary CLI failures rather than coercing to a wrong secret value.
    """
    from .config_control import normalize_secret_input

    stdin = sys.stdin
    buf = getattr(stdin, "buffer", None)
    if buf is not None:
        raw_bytes = buf.read()
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            from .config_control import ConfigValidationError, ConfigIssue

            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="BAD_STDIN_ENCODING",
                message=f"stdin is not valid UTF-8: {exc.reason}",
            )])
    else:
        # Text-only stream (e.g. io.StringIO used by tests).
        try:
            text = stdin.read()
        except UnicodeDecodeError as exc:
            from .config_control import ConfigValidationError, ConfigIssue

            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="BAD_STDIN_ENCODING",
                message=f"stdin is not valid UTF-8: {exc.reason}",
            )])
    return normalize_secret_input(text)


def _read_secret_from_getpass() -> str:
    """Read the secret from getpass (hidden echo, never logged)."""
    from .config_control import ConfigValidationError, ConfigIssue, normalize_secret_input

    try:
        value = getpass.getpass("api key: ")
    except (EOFError, KeyboardInterrupt):
        raise ConfigValidationError([ConfigIssue(
            field="secret",
            severity="error",
            code="INTERRUPTED_INPUT",
            message="secret input was interrupted",
        )])
    return normalize_secret_input(value)


def _run_set_key(args: argparse.Namespace, service: Any) -> int:
    """Set one or more apiKey/api_key fields atomically.

    The secret is *never* accepted via argv or echoed to stdout/stderr.
    The CLI re-routes the service's atomic guarantee: a single
    ``service.set_keys(targets, secret)`` call writes all targets in
    one transaction; if any validation fails, no disk write happens.
    """
    targets: list[str] = list(getattr(args, "targets", []) or [])
    use_stdin = bool(getattr(args, "stdin", False))

    if not targets:
        _err("error: at least one target is required (embedding|rerank|llm)")
        return 1

    try:
        secret = _read_secret_from_stdin() if use_stdin else _read_secret_from_getpass()
    except Exception as exc:
        return _handle_service_error(exc)

    # From here on, ``secret`` is local-only. We do not log it, we do not
    # echo it, and we do not store it beyond this function. The service
    # call below is the only place that touches disk.
    try:
        service.set_keys(targets=targets, secret=secret)
    except Exception as exc:
        return _handle_service_error(exc)
    finally:
        # Best-effort scrub of the local binding. CPython keeps strings
        # immutable so this can't actually clear the buffer, but we
        # drop the reference and let normal GC take over.
        secret = ""  # noqa: F841

    _emit("SET-KEY: PASS")
    _emit(f"  targets: {', '.join(targets)}")

    issues = service.validate()
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        _emit("VALIDATION: FAIL")
        for issue in errors:
            _emit(f"  - {issue}")
        return 1
    _emit("VALIDATION: PASS")
    return 0


# ----------------------------------------------------------------- set-toggle


def _run_set_toggle(args: argparse.Namespace, service: Any) -> int:
    """Flip a known feature toggle. Unknown toggles are rejected by the service."""
    name = getattr(args, "name", None)
    value = getattr(args, "value", None)

    if name is None or value is None:
        _err("error: set-toggle requires <name> <on|off>")
        return 1
    if value not in ("on", "off"):
        _err("error: toggle value must be 'on' or 'off'")
        return 1

    bool_val = value == "on"
    try:
        service.set_toggle(name=name, value=bool_val)
    except Exception as exc:
        return _handle_service_error(exc)

    _emit("SET-TOGGLE: PASS")
    _emit(f"  toggle: {name} = {bool_val}")
    issues = service.validate()
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        _emit("VALIDATION: FAIL")
        for issue in errors:
            _emit(f"  - {issue}")
        return 1
    _emit("VALIDATION: PASS")
    return 0


# --------------------------------------------------------------------- test


def _is_postgres_configured(service: Any) -> bool:
    """Return True when storage.pg has at least host + port + database + user."""
    try:
        data, issues = service.read_data()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    storage = data.get("storage")
    if not isinstance(storage, dict):
        return False
    pg = storage.get("pg")
    if not isinstance(pg, dict):
        return False
    return all(k in pg for k in ("host", "port", "database", "user"))


def _is_provider_configured(service: Any, target: str) -> bool:
    """Return True when the named provider has the minimum fields set."""
    try:
        data, _ = service.read_data()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False

    if target == "embedding":
        section = data.get("storage", {}).get("embed") if isinstance(data.get("storage"), dict) else None
        if not isinstance(section, dict):
            return False
        return bool(str(section.get("endpoint", "")).strip())
    if target == "rerank":
        section = data.get("storage", {}).get("rerank") if isinstance(data.get("storage"), dict) else None
        if not isinstance(section, dict):
            return False
        return bool(str(section.get("endpoint", "")).strip())
    if target == "llm":
        section = data.get("llm")
        if not isinstance(section, dict):
            return False
        return bool(str(section.get("provider", "")).strip()) and bool(str(section.get("model", "")).strip())
    return False


def _run_test_one(service: Any, target: str, skip_unconfigured: bool = False) -> int:
    """Run a single explicit provider/PG test. Returns 0 on ok, 1 on fail.

    When ``skip_unconfigured`` is True (used by ``config test all``),
    unconfigured providers print ``SKIP: NOT CONFIGURED`` and return 0
    (skip is not a failure for the aggregate target).

    When called explicitly (``config test embedding|rerank|llm|postgres``),
    an unconfigured target prints ``NOT CONFIGURED`` and returns 1 — the
    user asked for that exact target and we must refuse to silently
    succeed.
    """
    canonical = target.strip().lower()
    skip_label = "SKIP" if skip_unconfigured else "NOT CONFIGURED"
    skip_exit = 0 if skip_unconfigured else 1
    if canonical in ("embedding", "embed"):
        if not _is_provider_configured(service, "embedding"):
            _emit(f"{skip_label}: embedding endpoint missing")
            return skip_exit
        result = service.test_target("embedding")
    elif canonical == "rerank":
        if not _is_provider_configured(service, "rerank"):
            _emit(f"{skip_label}: rerank endpoint missing")
            return skip_exit
        result = service.test_target("rerank")
    elif canonical == "llm":
        if not _is_provider_configured(service, "llm"):
            _emit(f"{skip_label}: llm provider or model missing")
            return skip_exit
        result = service.test_target("llm")
    elif canonical in ("postgres", "pg"):
        if not _is_postgres_configured(service):
            _emit(f"{skip_label}: postgres connection fields missing")
            return skip_exit
        result = service.test_target("postgres")
    else:
        _err(
            f"error: unknown test target {target!r}; "
            "supported: embedding, rerank, llm, postgres, all"
        )
        return 1

    # ``result`` is a safe dict produced by the service. Print it as-is.
    target_name = str(result.get("target", target))
    ok = bool(result.get("ok"))
    error = result.get("error")
    _emit(f"TEST: {target_name} -> {'OK' if ok else 'FAIL'}")
    for k in sorted(result.keys()):
        if k in ("target", "ok"):
            continue
        v = result.get(k)
        # ``error`` from the service is already safe.
        if v is None or v == "":
            continue
        _emit(f"  {k}: {v}")
    if not ok and not error:
        _err(f"  error: test failed (no detail returned by service)")
        return 1
    return 0 if ok else 1


def _run_test_all(args: argparse.Namespace, service: Any) -> int:
    """Run every provider + postgres. Validate first.

    Postgres is always attempted if configured. Provider targets that
    are not configured skip rather than fail; provider targets that
    *are* configured but fail the live test return a non-zero status.
    """
    try:
        issues = service.validate()
        errors = [i for i in issues if i.severity == "error"]
    except Exception as exc:
        return _handle_service_error(exc)
    if errors:
        _emit("VALIDATION: FAIL")
        for issue in errors:
            _emit(f"  - {issue}")
        return 1
    _emit("VALIDATION: PASS")

    results: list[int] = []
    for tgt in ("embedding", "rerank", "llm", "postgres"):
        results.append(_run_test_one(service, tgt, skip_unconfigured=True))
    return 0 if all(r == 0 for r in results) else 1


def _run_test(args: argparse.Namespace, service: Any) -> int:
    target = getattr(args, "target", None)
    if not target:
        _err("error: test requires a target: embedding|rerank|llm|postgres|all")
        return 1
    canonical = target.strip().lower()
    if canonical == "all":
        return _run_test_all(args, service)
    return _run_test_one(service, target)


# ---------------------------------------------------------------- argparse


def add_config_parser(subparsers: Any) -> Any:
    """Register ``config`` and its subcommands on ``subparsers``.

    Returns the top-level ``config`` subparser so callers can chain if
    they want. Every subcommand accepts ``--profile``.
    """
    config_p = subparsers.add_parser(
        "config",
        help="inspect, validate and edit v3-core config (thin CLI)",
        description=(
            "Read, validate, and edit v3-core config via the "
            "ConfigControlService. Secrets are never accepted via argv."
        ),
    )
    config_subs = config_p.add_subparsers(dest="config_command")

    # show ----------------------------------------------------------------
    show_p = config_subs.add_parser("show", help="print redacted config report")
    show_p.add_argument(
        "--profile", default="default",
        help="v3-core profile (default: default)",
    )
    show_p.add_argument(
        "--json", action="store_true",
        help="emit the same redacted report as JSON",
    )
    show_p.set_defaults(_runner="show")

    # validate ------------------------------------------------------------
    validate_p = config_subs.add_parser(
        "validate", help="offline validate the config (no network, no providers)"
    )
    validate_p.add_argument(
        "--profile", default="default",
        help="v3-core profile (default: default)",
    )
    validate_p.set_defaults(_runner="validate")

    # set-provider --------------------------------------------------------
    setp_p = config_subs.add_parser(
        "set-provider",
        help="set endpoint/model on one provider target",
    )
    setp_p.add_argument(
        "target", choices=("embedding", "rerank", "llm"),
        help="which provider target to update",
    )
    setp_p.add_argument(
        "--endpoint", default=None,
        help="new endpoint URL (for llm this maps to base_url)",
    )
    setp_p.add_argument(
        "--model", default=None,
        help="new model identifier",
    )
    setp_p.add_argument(
        "--profile", default="default",
        help="v3-core profile (default: default)",
    )
    setp_p.set_defaults(_runner="set_provider")

    # set-key -------------------------------------------------------------
    setk_p = config_subs.add_parser(
        "set-key",
        help="set api key for one or more targets (secret via stdin or prompt)",
    )
    setk_p.add_argument(
        "targets", nargs="+",
        help="one or more of: embedding rerank llm",
    )
    setk_p.add_argument(
        "--stdin", action="store_true",
        help="read the secret from stdin (raw bytes, UTF-8, "
             "CRLF-normalised); without --stdin the CLI prompts via getpass",
    )
    setk_p.add_argument(
        "--profile", default="default",
        help="v3-core profile (default: default)",
    )
    setk_p.set_defaults(_runner="set_key")

    # set-toggle ----------------------------------------------------------
    sett_p = config_subs.add_parser(
        "set-toggle", help="flip a known feature toggle on/off",
    )
    sett_p.add_argument(
        "name",
        help=(
            "toggle path; supported: e1.enabled, tkg.enabled, "
            "prefetch.enabled, prefetch.dual_path, "
            "prefetch.include_message_vector"
        ),
    )
    sett_p.add_argument("value", choices=("on", "off"))
    sett_p.add_argument(
        "--profile", default="default",
        help="v3-core profile (default: default)",
    )
    sett_p.set_defaults(_runner="set_toggle")

    # test ----------------------------------------------------------------
    test_p = config_subs.add_parser(
        "test",
        help="explicitly run one live provider/PG probe; "
             "never triggered by show/validate",
    )
    test_p.add_argument(
        "target",
        help="which target to test: embedding|rerank|llm|postgres|all",
    )
    test_p.add_argument(
        "--profile", default="default",
        help="v3-core profile (default: default)",
    )
    test_p.set_defaults(_runner="test")

    return config_p


# --------------------------------------------------------------- dispatcher


_RUNNERS = {
    "show": _run_show,
    "validate": _run_validate,
    "set_provider": _run_set_provider,
    "set_key": _run_set_key,
    "set_toggle": _run_set_toggle,
    "test": _run_test,
}


def run_config(args: argparse.Namespace) -> int:
    """Dispatch one ``config ...`` invocation to the right runner.

    Returns 0 on success, non-zero on every failure. Never raises.
    Never prints a traceback.
    """
    runner_name = getattr(args, "_runner", None)
    if runner_name is None:
        _err("error: no config subcommand given")
        _err("       try: python -m v3core config --help")
        return 1
    runner = _RUNNERS.get(runner_name)
    if runner is None:
        _err(f"error: unknown config subcommand {runner_name!r}")
        return 2
    try:
        service = _make_service(args)
    except Exception as exc:
        return _handle_service_error(exc)
    try:
        return runner(args, service)
    except Exception as exc:
        return _handle_service_error(exc)


__all__ = [
    "add_config_parser",
    "run_config",
]