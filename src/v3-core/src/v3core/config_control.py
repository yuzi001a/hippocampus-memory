"""v3-core config control plane — validation + safe mutation service (unit 1+2).

Public surface:
    validate(profile=None, *, env=None, path=None) -> list[ConfigIssue]
    ConfigIssue, ConfigValidationError, ConfigControlService,
    normalize_secret_input

Resolution order for the config file:
    1. ``V3CORE_CONFIG`` env var (must be absolute)
    2. ``v3core._find_config(profile)`` if importable
    3. ``~/.v3-core/profiles/<profile>/config.yaml``

Only stdlib + PyYAML. No CLI, no provider dispatch.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    import yaml  # PyYAML
except ImportError as e:  # pragma: no cover
    raise ImportError("config_control requires PyYAML") from e


# ---------------------------------------------------------------------------
# Issues / errors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigIssue:
    field: str = ""           # dotted path or "_raw" sentinel
    severity: str = "error"   # "error" | "warning"
    code: str = ""            # short token, e.g. C0_CONTROL_CHAR
    message: str = ""        # human-readable, never echoes raw line
    line: int | None = None
    column: int | None = None
    byte_offset: int | None = None

    def __str__(self) -> str:
        bits = [f"[{self.severity}]", self.code]
        loc: list[str] = []
        if self.line is not None:
            loc.append(f"line {self.line}")
            if self.column is not None:
                loc.append(f"col {self.column}")
        if loc:
            bits.append("/".join(loc))
        if self.field:
            bits.append(f"({self.field})")
        return f"{' '.join(b for b in bits if b)}: {self.message}"


class ConfigValidationError(Exception):
    def __init__(self, issues: list[ConfigIssue]):
        self.issues = list(issues)
        super().__init__(
            "; ".join(str(i) for i in issues) or "config validation failed"
        )


# ---------------------------------------------------------------------------
# Profile name
# ---------------------------------------------------------------------------

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_profile_name(profile: Any) -> str | None:
    """Return a human error if the profile name is unsafe, else None."""
    if profile is None:
        return "profile name is required when V3CORE_CONFIG is not set"
    if not isinstance(profile, str):
        return f"profile name must be a string, got {type(profile).__name__}"
    if not profile:
        return "profile name must not be empty"
    if profile in {".", ".."}:
        return "profile name must not be '.' or '..'"
    if "/" in profile or "\\" in profile or os.sep in profile:
        return "profile name must not contain path separators"
    if os.path.isabs(profile):
        return "profile name must not be an absolute path"
    if not _PROFILE_NAME_RE.match(profile):
        return (
            "profile name must match [A-Za-z0-9._-]{1,64} "
            f"(got {profile!r})"
        )
    return None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _try_legacy_find_config(profile: str) -> Path | None:
    try:
        import v3core  # type: ignore
    except Exception:
        return None
    fn = getattr(v3core, "_find_config", None)
    if fn is None:
        return None
    try:
        result = fn(profile)
    except Exception:
        return None
    if result is None:
        return None
    try:
        return Path(result)
    except TypeError:
        return None


def _resolve_config_path(
    profile: str | None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, str | None]:
    """Return ``(path, source_or_error)``.

    ``source_or_error`` describes which rule matched when path is set,
    or carries the failure reason when path is None.
    """
    env_map = env if env is not None else os.environ
    explicit = env_map.get("V3CORE_CONFIG")
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            return None, "V3CORE_CONFIG must be an absolute path"
        return p, "env:V3CORE_CONFIG"

    if profile is None:
        return None, _validate_profile_name(None)

    name_err = _validate_profile_name(profile)
    if name_err:
        return None, name_err

    legacy = _try_legacy_find_config(profile)
    if legacy is not None:
        return legacy, "legacy:_find_config"

    default = Path.home() / ".v3-core" / "profiles" / profile / "config.yaml"
    return default, "default"


# ---------------------------------------------------------------------------
# Control-character scan (raw text + per-value recursive)
# ---------------------------------------------------------------------------

# Allowed C0: TAB(09), LF(0A), CR(0D). Everything else in 00..1F and DEL(7F) is rejected.
_ALLOWED_CONTROL = frozenset({0x09, 0x0A, 0x0D})


def _read_text(path: Path) -> str:
    # Preserve original line endings (CRLF on Windows-edited YAML files).
    # ``Path.read_text`` uses universal newlines which collapses CRLF to LF,
    # so a round-trip write would silently normalize the file. Use binary
    # newline mode (``newline=""``) to keep line endings byte-exact, and
    # strip a UTF-8 BOM by hand to match the previous ``read_text`` behaviour.
    with open(path, "r", encoding="utf-8", errors="strict", newline="") as f:
        data = f.read()
    if data.startswith("\ufeff"):
        data = data[1:]
    return data


def _scan_control_chars(text: str) -> list[ConfigIssue]:
    """Reject C0 control characters (except CR/LF/TAB) and DEL.

    U+0016 is reported with line/column and a best-effort ``field`` derived
    from the surrounding YAML mapping stack at that line (e.g.
    ``storage.embed.apiKey``). The offending line content is never echoed.
    """
    issues: list[ConfigIssue] = []
    # Pre-compute the dotted path stack for every line index so we can map
    # a byte offset back to a logical field without re-walking the text.
    field_at_line = _field_path_for_each_line(text)
    line = 1
    col = 1
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp == 0x0A:
            line += 1
            col = 1
            continue
        if cp == 0x0D:
            col = 1
            continue
        if cp < 0x20 or cp == 0x7F:
            issues.append(
                ConfigIssue(
                    field=field_at_line.get(line, "_raw"),
                    severity="error",
                    code="C0_CONTROL_CHAR",
                    message=(
                        f"refusing control character U+{cp:04X} "
                        "(only TAB/LF/CR are permitted)"
                    ),
                    line=line,
                    column=col,
                    byte_offset=i,
                )
            )
        col += 1
    return issues


def _field_path_for_each_line(text: str) -> dict[int, str]:
    """Map 1-based line numbers to the YAML dotted path covering that line.

    Blank lines and comment lines inherit the path of the most recently
    seen mapping header. Lines whose own key forms the leaf of the path
    get the leaf appended. Used by the raw control-char scanner to label
    bare U+0016 occurrences with a safe ``storage.embed.apiKey``-style
    field name rather than the bare ``_raw`` sentinel.
    """
    # 1-based line numbers in this dict match the line counter in
    # _scan_control_chars.
    out: dict[int, str] = {}
    current: list[str] = []
    last_seen_line = 0
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            # Blank or comment: inherit the current path so offsets here
            # are still labelled usefully.
            out[line_no] = ".".join(current) if current else "_raw"
            continue
        m = _FIELD_RE.match(raw)
        if not m:
            out[line_no] = ".".join(current) if current else "_raw"
            continue
        indent = len(m.group("indent").expandtabs(4))
        key = m.group("key")
        # Pop until current depth < this indent.
        target_depth = indent // 2
        while len(current) > target_depth:
            current.pop()
        # If the line's rest has an inline shape (non-empty, unquoted, not
        # | / >), don't extend the path with this key — it's a leaf, not a
        # mapping header.
        rest = m.group("rest").strip()
        inline_leaf = bool(rest) and not _is_quoted_yaml_scalar(rest) \
            and rest not in ("|", ">")
        if not inline_leaf:
            # Map any prior depth to ensure same length as target_depth.
            while len(current) < target_depth:
                current.append("?")
            if len(current) == target_depth:
                current.append(key)
            else:
                # Unexpected depth drift — clamp.
                current = current[:target_depth]
                current.append(key)
            path = ".".join(current)
        else:
            # Inline leaf: the path that *contains* this key is one shorter.
            path_parts = list(current)
            while len(path_parts) > target_depth:
                path_parts.pop()
            if len(path_parts) == target_depth:
                path_parts.append(key)
            path = ".".join(path_parts) if path_parts else "_raw"
        out[line_no] = path
        last_seen_line = line_no
    return out


def _scan_control_chars_in_value(value: Any, field: str) -> list[ConfigIssue]:
    """Walk a parsed YAML value and emit a C0/DEL issue per offending string.

    The ``field`` is the dotted logical path (e.g. ``storage.embed.apiKey``).
    The message only contains the field name, code-point, and the byte_offset
    inside the string. The actual string bytes are never echoed.
    """
    issues: list[ConfigIssue] = []
    if isinstance(value, str):
        for i, ch in enumerate(value):
            cp = ord(ch)
            if cp in _ALLOWED_CONTROL:
                continue
            if cp < 0x20 or cp == 0x7F:
                issues.append(
                    ConfigIssue(
                        field=field,
                        severity="error",
                        code="C0_CONTROL_CHAR",
                        message=(
                            f"refusing control character U+{cp:04X} "
                            "in string value (only TAB/LF/CR are permitted)"
                        ),
                        line=None,
                        column=None,
                        byte_offset=i,
                    )
                )
        return issues
    if isinstance(value, list):
        for idx, item in enumerate(value):
            issues.extend(
                _scan_control_chars_in_value(item, f"{field}[{idx}]")
            )
        return issues
    if isinstance(value, Mapping):
        for k, v in value.items():
            sub = f"{field}.{k}" if field else str(k)
            issues.extend(_scan_control_chars_in_value(v, sub))
        return issues
    return issues


def _all_control_char_issues(
    raw_text: str, parsed: Any
) -> list[ConfigIssue]:
    """Combine raw-text and per-value C0 scans.

    A literal U+0016 that survives YAML parsing as part of a string (e.g.
    via ``\\x16`` or a YAML escape) is caught by the per-value pass. Bare
    U+0016 in the raw stream is caught by the raw pass.
    """
    issues = _scan_control_chars(raw_text)
    if parsed is not None:
        issues.extend(_scan_control_chars_in_value(parsed, ""))
    return issues


# ---------------------------------------------------------------------------
# Endpoint URL redaction (safe formatter for show(redact=False))
# ---------------------------------------------------------------------------

def _safe_endpoint_url(endpoint: str) -> str:
    """Return a redacted-but-informative view of an endpoint URL.

    - Strips ``query`` and ``fragment`` entirely (never echoed in show()).
    - Masks userinfo: ``user:pass@host`` becomes ``***@host``.
    - Returns ``""`` when parsing fails or the value is not a string.
    """
    if not isinstance(endpoint, str):
        return ""
    s = endpoint.strip()
    if not s:
        return ""
    # urlsplit accepts URLs without scheme; fall back gracefully.
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(s)
    except Exception:
        return _endpoint_redacted_fallback(s)
    # Mask userinfo (anything before @ in netloc).
    netloc = parts.netloc
    userinfo = ""
    host = netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
    masked_userinfo = "***" if userinfo else ""
    new_netloc = (
        f"{masked_userinfo}@{host}" if masked_userinfo else host
    )
    # Always drop query and fragment.
    try:
        cleaned = urlunsplit((
            parts.scheme,
            new_netloc,
            parts.path,
            "",  # query — always stripped
            "",  # fragment — always stripped
        ))
    except Exception:
        return _endpoint_redacted_fallback(s)
    return cleaned


def _endpoint_redacted_fallback(s: str) -> str:
    """Last-resort masking when urlsplit fails.

    Drops any ``?query`` and ``#fragment`` and masks ``user:pass@`` shapes.
    """
    out = s
    # Strip fragment first, then query.
    for sep in ("#", "?"):
        idx = out.find(sep)
        if idx != -1:
            out = out[:idx]
    if "@" in out:
        # Mask everything before the last ``@``.
        head, _at, tail = out.rpartition("@")
        # Only treat as userinfo if it contains a colon (user:pass) or no
        # slashes in the head (so we don't mistreat a path with @ in it).
        if ":" in head or "/" not in head:
            out = "***@" + tail
    return out


# ---------------------------------------------------------------------------
# YAML parse
# ---------------------------------------------------------------------------

def _parse_yaml(text: str) -> tuple[Any, ConfigIssue | None]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        col = mark.column + 1 if mark is not None else None
        raw_problem = (
            getattr(e, "problem", None) or str(e) or "YAML parse failed"
        )
        # Sanitize: PyYAML's ``problem`` string can include snippets of the
        # offending input (including control characters or other bytes the
        # caller may not want echoed). Strip C0/DEL/TAB to a single space
        # and collapse runs, then cap the length so an attacker can't blow
        # up the diagnostic.
        sanitized = _sanitize_diagnostic_snippet(raw_problem)
        return None, ConfigIssue(
            field="_raw",
            severity="error",
            code="YAML_PARSE_ERROR",
            message=sanitized,
            line=line,
            column=col,
        )
    return data, None


def _sanitize_diagnostic_snippet(s: str, max_len: int = 240) -> str:
    """Make a YAML diagnostic message safe to surface.

    - Removes C0 control characters and DEL entirely (so we never echo
      the raw bytes that triggered the parse error).
    - Replaces runs of whitespace with a single space and trims ends.
    - Truncates to ``max_len`` chars.
    """
    if not s:
        return "YAML parse failed"
    chars: list[str] = []
    prev_space = False
    for ch in s:
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:
            # Treat any control char (incl. TAB/CR/LF) as whitespace,
            # collapsing with surrounding whitespace.
            if not prev_space:
                chars.append(" ")
                prev_space = True
            continue
        if ch.isspace():
            if not prev_space:
                chars.append(" ")
                prev_space = True
            continue
        chars.append(ch)
        prev_space = False
    out = "".join(chars).strip()
    if len(out) > max_len:
        out = out[:max_len].rstrip() + "..."
    return out or "YAML parse failed"


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

# Sections that MUST be a mapping if present.
_REQUIRED_MAPPING_SECTIONS = (
    "storage",
    "llm",
)

# Nested sections under storage that MUST be mappings if present.
_STORAGE_MAPPING_SECTIONS = ("pg", "embed", "rerank")

# When present, these keys must be strings.
_STRING_KEYS = frozenset({
    "endpoint", "model", "apiKey", "api_key",
    "provider", "base_url",
    "password", "host", "database", "user",
})


def _is_absolute_path_like(value: str) -> bool:
    """Return True if *value* is an absolute path (POSIX or Windows)."""
    if not value:
        return False
    if os.path.isabs(value):
        return True
    # Windows drive letter (C:\ or C:/) or UNC (\\server\share).
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    if value.startswith("\\\\") or value.startswith("//"):
        return True
    return False


def _check_section_types(data: Any, issues: list[ConfigIssue]) -> None:
    if not isinstance(data, Mapping):
        issues.append(
            ConfigIssue(
                field="",
                severity="error",
                code="BAD_ROOT_TYPE",
                message=(
                    "top-level config must be a mapping, got "
                    f"{type(data).__name__}"
                ),
            )
        )
        return
    for name in _REQUIRED_MAPPING_SECTIONS:
        if name not in data:
            continue
        value = data[name]
        if not isinstance(value, Mapping):
            issues.append(
                ConfigIssue(
                    field=name,
                    severity="error",
                    code="BAD_SECTION_TYPE",
                    message=(
                        f"{name!r} must be a mapping, got "
                        f"{type(value).__name__}"
                    ),
                )
            )
    storage = data.get("storage")
    if isinstance(storage, Mapping):
        for name in _STORAGE_MAPPING_SECTIONS:
            if name not in storage:
                continue
            value = storage[name]
            if not isinstance(value, Mapping):
                issues.append(
                    ConfigIssue(
                        field=f"storage.{name}",
                        severity="error",
                        code="BAD_SECTION_TYPE",
                        message=(
                            f"storage.{name!r} must be a mapping, got "
                            f"{type(value).__name__}"
                        ),
                    )
                )


def _check_string_keys(section: Mapping[str, Any], field_prefix: str,
                       issues: list[ConfigIssue]) -> None:
    for key, value in section.items():
        if key in _STRING_KEYS:
            if not isinstance(value, str):
                issues.append(
                    ConfigIssue(
                        field=f"{field_prefix}.{key}",
                        severity="error",
                        code="BAD_TYPE",
                        message=(
                            f"{field_prefix}.{key} must be a string, got "
                            f"{type(value).__name__}"
                        ),
                    )
                )


def _check_base_path(data: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    if "basePath" not in data:
        return
    bp = data["basePath"]
    if not isinstance(bp, str):
        issues.append(
            ConfigIssue(
                field="basePath",
                severity="error",
                code="BAD_TYPE",
                message=f"basePath must be a string, got {type(bp).__name__}",
            )
        )
        return
    # An empty string ``basePath: ""`` is the runtime-default "unspecified"
    # sentinel and is allowed; non-string values above are still rejected, and
    # any non-empty relative path is still flagged NOT_ABSOLUTE below.
    if not bp.strip():
        return
    if not _is_absolute_path_like(bp):
        issues.append(
            ConfigIssue(
                field="basePath",
                severity="error",
                code="NOT_ABSOLUTE",
                message=(
                    "basePath must be an absolute path "
                    "(POSIX or Windows)"
                ),
            )
        )


def _check_pg(data: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    storage = data.get("storage")
    if not isinstance(storage, Mapping):
        return
    pg = storage.get("pg")
    if pg is None:
        return
    if not isinstance(pg, Mapping):
        return  # type error already reported by section check
    _check_string_keys(pg, "storage.pg", issues)
    if "port" not in pg:
        return
    port = pg["port"]
    # bool is a subclass of int — reject explicitly.
    if isinstance(port, bool) or not isinstance(port, int):
        issues.append(
            ConfigIssue(
                field="storage.pg.port",
                severity="error",
                code="BAD_TYPE",
                message=(
                    "storage.pg.port must be an integer, got "
                    f"{type(port).__name__}"
                ),
            )
        )
        return
    if not (1 <= port <= 65535):
        issues.append(
            ConfigIssue(
                field="storage.pg.port",
                severity="error",
                code="PORT_OUT_OF_RANGE",
                message=f"storage.pg.port must be in 1..65535, got {port}",
            )
        )


def _check_storage_subkeys(data: Mapping[str, Any], issues: list[ConfigIssue]
                            ) -> None:
    """Check string-typed keys in storage.embed/rerank without forcing
    any specific key to exist (embed/rerank may legitimately be partial)."""
    storage = data.get("storage")
    if not isinstance(storage, Mapping):
        return
    for sub in ("embed", "rerank"):
        section = storage.get(sub)
        if not isinstance(section, Mapping):
            continue
        _check_string_keys(section, f"storage.{sub}", issues)


def _check_llm(data: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    llm = data.get("llm")
    if llm is None:
        return
    if not isinstance(llm, Mapping):
        return
    _check_string_keys(llm, "llm", issues)


def _validate_structure(data: Any) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    _check_section_types(data, issues)
    if isinstance(data, Mapping):
        _check_base_path(data, issues)
        _check_pg(data, issues)
        _check_storage_subkeys(data, issues)
        _check_llm(data, issues)
    return issues


# ---------------------------------------------------------------------------
# Combined validate pipeline
# ---------------------------------------------------------------------------

def _validate_text(text: str) -> tuple[Any, list[ConfigIssue]]:
    """Parse + structural-check *text* and return (data, issues)."""
    issues: list[ConfigIssue] = []
    data, parse_issue = _parse_yaml(text)
    if parse_issue is not None:
        issues.append(parse_issue)
        return data, issues
    issues.extend(_validate_structure(data))
    return data, issues


def validate(
    profile: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> list[ConfigIssue]:
    """Validate a v3-core config and return a list of human-readable issues.

    An empty list means OK. Problems are reported, not raised — wrap the
    result with ``ConfigValidationError`` if you need an exception.
    """
    issues: list[ConfigIssue] = []

    if path is None:
        path, source_or_err = _resolve_config_path(profile, env)
        if path is None:
            issues.append(
                ConfigIssue(
                    field="",
                    severity="error",
                    code="CONFIG_NOT_FOUND",
                    message=source_or_err or "could not resolve config path",
                )
            )
            return issues

    if not path.exists():
        issues.append(
            ConfigIssue(
                field="",
                severity="error",
                code="CONFIG_NOT_FOUND",
                message=f"config file does not exist: {path}",
            )
        )
        return issues

    try:
        text = _read_text(path)
    except UnicodeDecodeError as e:
        issues.append(
            ConfigIssue(
                field="_raw",
                severity="error",
                code="BAD_ENCODING",
                message=f"file is not valid UTF-8: {e.reason} at byte {e.start}",
                byte_offset=e.start,
            )
        )
        return issues
    except OSError as e:
        issues.append(
            ConfigIssue(
                field="",
                severity="error",
                code="READ_ERROR",
                message=f"could not read config file: {e}",
            )
        )
        return issues

    data, structural = _validate_text(text)
    # C0/DEL scan operates on raw text (catches bare U+0016) AND on parsed
    # values (catches YAML escape sequences that materialise as control chars).
    issues.extend(_all_control_char_issues(text, data))
    issues.extend(structural)
    return issues


# ---------------------------------------------------------------------------
# Secrets stdin helper
# ---------------------------------------------------------------------------

def normalize_secret_input(raw: Any) -> str:
    """Strip a single trailing newline from stdin-fed secrets.

    Rules:
      * Input must be a string.
      * A single trailing CRLF, LF, or CR is removed.
      * Two or more trailing CR/LF are rejected (suggests the caller did not
        terminate the read cleanly).
      * Empty / whitespace-only secrets are rejected.
      * Any embedded C0 control character (TAB excluded) or DEL is rejected.

    The function never logs or echoes the secret. Exceptions contain only
    a coarse reason, never a snippet of the value.
    """
    if raw is None:
        raise ConfigValidationError([ConfigIssue(
            field="secret",
            severity="error",
            code="EMPTY_SECRET",
            message="secret input was None",
        )])
    if not isinstance(raw, str):
        raise ConfigValidationError([ConfigIssue(
            field="secret",
            severity="error",
            code="BAD_SECRET_TYPE",
            message=(
                "secret must be a string from stdin, got "
                f"{type(raw).__name__}"
            ),
        )])

    # Strip exactly one trailing line terminator:
    #   CRLF (\r\n) -> single terminator, strip both
    #   LF (\n) only -> single terminator
    #   CR (\r) only -> single terminator
    # Anything more than one terminator (any combination of CR/LF) is rejected
    # as multi-line input. We do this by counting trailing CR/LF chars and
    # requiring the count be exactly 1 OR exactly 2 (CRLF pair).
    s = raw
    n = len(s)
    # Count trailing CR/LF characters.
    trailing_count = 0
    while trailing_count < n and s[n - 1 - trailing_count] in ("\n", "\r"):
        trailing_count += 1
    # Acceptable: exactly 1 (LF or CR) OR exactly 2 when the pair is CRLF.
    if trailing_count == 0:
        # No trailing newline — still acceptable.
        stripped = s
    elif trailing_count == 1:
        stripped = s[:-1]
    elif trailing_count == 2 and s.endswith("\r\n"):
        stripped = s[:-2]
    else:
        raise ConfigValidationError([ConfigIssue(
            field="secret",
            severity="error",
            code="SECRET_MULTI_NEWLINE",
            message=(
                "secret has multiple trailing CR/LF; "
                "stdin must be a single line"
            ),
        )])
    s = stripped

    # Reject embedded control chars / DEL. TAB is NOT a legal secret character.
    # LF/CR were only legal as the trailing terminator stripped above.
    for i, ch in enumerate(s):
        cp = ord(ch)
        if cp == 0x09:
            # TAB is rejected — secrets must not contain TAB.
            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="SECRET_CONTROL_CHAR",
                message=(
                    f"refusing control character U+{cp:04X} in secret"
                ),
                byte_offset=i,
            )])
        if cp in _ALLOWED_CONTROL:
            # LF/CR embedded inside (not trailing) is rejected.
            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="SECRET_CONTROL_CHAR",
                message=(
                    f"refusing control character U+{cp:04X} in secret"
                ),
                byte_offset=i,
            )])
        if cp < 0x20 or cp == 0x7F:
            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="SECRET_CONTROL_CHAR",
                message=(
                    f"refusing control character U+{cp:04X} in secret"
                ),
                byte_offset=i,
            )])

    if not s.strip():
        raise ConfigValidationError([ConfigIssue(
            field="secret",
            severity="error",
            code="EMPTY_SECRET",
            message="secret is empty after whitespace strip",
        )])

    return s


# ---------------------------------------------------------------------------
# Known-path YAML scalar mutation (preserves unknown fields, comments, order)
# ---------------------------------------------------------------------------

# Mutation targets supported by atomic_update / set_* helpers.
_KNOWN_TARGETS = {
    # legacy alias → canonical
    "embedding": ("storage", "embed"),
    "rerank": ("storage", "rerank"),
    "llm": ("llm",),
}

# Field-name aliases for known targets. The first match in each tuple is the
# canonical name written back to disk; subsequent names are accepted as input.
_TARGET_FIELD_ALIASES: dict[tuple[str, ...], tuple[tuple[str, ...], ...]] = {
    ("llm",): (
        ("provider",),
        ("model",),
        ("apiKey", "api_key"),
        ("base_url", "baseUrl"),
    ),
    ("storage", "embed"): (
        ("endpoint",),
        ("model",),
        ("apiKey", "api_key"),
        ("proxy",),
        ("dim",),
    ),
    ("storage", "rerank"): (
        ("endpoint",),
        ("apiKey", "api_key"),
        ("proxy",),
        ("model",),
    ),
}


def _resolve_target_path(target: str) -> tuple[str, ...]:
    if target not in _KNOWN_TARGETS:
        raise ConfigValidationError([ConfigIssue(
            field=target,
            severity="error",
            code="UNKNOWN_TARGET",
            message=(
                f"unknown mutation target {target!r}; "
                f"known targets: {sorted(_KNOWN_TARGETS)}"
            ),
        )])
    return _KNOWN_TARGETS[target]


def _resolve_field_alias(parent_path: tuple[str, ...], field: str) -> str:
    aliases = _TARGET_FIELD_ALIASES.get(parent_path)
    if aliases is None:
        return field
    for group in aliases:
        if field in group:
            # Caller-supplied field is already a member of an alias group
            # (e.g. ``api_key`` vs canonical ``apiKey``). Preserve it so the
            # existing-alias detection in ``_existing_secret_alias`` can
            # pin the in-file spelling; only fall back to canonical when
            # the caller asked for something not in any group.
            return field
    return field


# Ruamel-style round trip is not available (only PyYAML); preserve by patching
# the raw text rather than re-serialising the parsed tree.

_FIELD_RE = re.compile(
    r"""(?mx)
    ^(?P<indent>[ \t]*)
    (?P<key>[A-Za-z_][A-Za-z0-9_\-]*)    # YAML bare key (no quotes)
    \s*:\s*
    (?P<rest>.*)$
    """
)


def _is_quoted_yaml_scalar(rest: str) -> bool:
    s = rest.lstrip()
    return s.startswith('"') or s.startswith("'")


def _yaml_double_quote(value: str) -> str:
    """Return one deterministic double-quoted YAML scalar."""
    escaped: list[str] = []
    for ch in value:
        cp = ord(ch)
        if ch == "\\":
            escaped.append("\\\\")
        elif ch == '"':
            escaped.append('\\"')
        elif ch == "\t":
            escaped.append("\\t")
        elif ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        elif cp < 0x20 or cp == 0x7F:
            escaped.append(f"\\u{cp:04x}")
        else:
            escaped.append(ch)
    return '"' + "".join(escaped) + '"'


def _safe_yaml_scalar(value: Any) -> str:
    """Serialize a scalar without changing its PyYAML semantic value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if not isinstance(value, str):
        return str(value)
    if value and value == value.strip() and "\n" not in value and "\r" not in value:
        try:
            probe = yaml.safe_load(f"__value__: {value}\n")
        except yaml.YAMLError:
            probe = None
        if (
            isinstance(probe, Mapping)
            and isinstance(probe.get("__value__"), str)
            and probe["__value__"] == value
        ):
            return value
    return _yaml_double_quote(value)


def _scalar_replacement_lines(
    rest: str, value: Any
) -> tuple[str, bool]:
    """Replace one scalar while preserving its trailing inline comment."""
    stripped = rest.lstrip()
    indent = rest[: len(rest) - len(stripped)]

    if stripped.startswith('"') or stripped.startswith("'"):
        quote = stripped[0]
        end = 1
        while end < len(stripped):
            c = stripped[end]
            if c == "\\" and quote == '"' and end + 1 < len(stripped):
                end += 2
                continue
            if c == quote:
                break
            end += 1
        if end >= len(stripped):
            raise ConfigValidationError([ConfigIssue(
                field="_raw",
                severity="error",
                code="UNCLOSED_QUOTE",
                message="could not locate closing quote for inline scalar",
            )])
        tail = stripped[end + 1 :]
        if isinstance(value, str) and quote == '"':
            return f"{indent}{_yaml_double_quote(value)}{tail}", True
        if isinstance(value, str) and quote == "'":
            return f"{indent}'{value.replace(chr(39), chr(39) * 2)}'{tail}", True
        return f"{indent}{_safe_yaml_scalar(value)}{tail}", True

    comment = ""
    body = stripped
    if " #" in stripped:
        body, comment = stripped.split(" #", 1)
        comment = " #" + comment
    elif stripped.startswith("#"):
        body = ""
        comment = "#" + stripped[1:]
    body = body.rstrip()
    return f"{indent}{_safe_yaml_scalar(value)}{comment}", body != ""


@dataclass
class _YamlLine:
    raw: str
    indent: int
    key: str | None
    rest: str


def _tokenise_yaml_lines(text: str) -> list[_YamlLine]:
    lines: list[_YamlLine] = []
    for raw in text.splitlines(keepends=False):
        # Comments and blank lines carry no key/indent info.
        if not raw.strip() or raw.lstrip().startswith("#"):
            lines.append(_YamlLine(raw=raw, indent=-1, key=None, rest=""))
            continue
        m = _FIELD_RE.match(raw)
        if not m:
            lines.append(_YamlLine(raw=raw, indent=-1, key=None, rest=""))
            continue
        indent = len(m.group("indent").expandtabs(4))
        key = m.group("key")
        rest = m.group("rest")
        lines.append(_YamlLine(raw=raw, indent=indent, key=key, rest=rest))
    return lines


def _block_end_index(lines: list[_YamlLine], header_idx: int) -> int:
    """Return the exclusive range end owned by one exact section header."""
    parent_indent = lines[header_idx].indent
    for idx in range(header_idx + 1, len(lines)):
        line = lines[idx]
        if line.key is not None and line.indent <= parent_indent:
            return idx
    return len(lines)


def _path_issue(path: tuple[str, ...], code: str, message: str) -> ConfigValidationError:
    return ConfigValidationError([ConfigIssue(
        field=".".join(path), severity="error", code=code, message=message
    )])


def _resolve_block(
    lines: list[_YamlLine],
    path: tuple[str, ...],
    *,
    require: bool,
) -> tuple[int | None, int, int]:
    """Resolve a mapping path through exact direct-parent ranges."""
    if not path:
        return None, 0, len(lines)
    parent_idx: int | None = None
    block_start = 0
    block_end = len(lines)
    for depth, segment in enumerate(path):
        expected_indent = 0 if parent_idx is None else lines[parent_idx].indent + 2
        matches = [
            idx for idx in range(block_start, block_end)
            if lines[idx].key == segment and lines[idx].indent == expected_indent
        ]
        dotted = ".".join(path[: depth + 1])
        if len(matches) > 1:
            raise _path_issue(
                path[: depth + 1],
                "AMBIGUOUS_SECTION",
                f"multiple direct sections named {segment!r} under "
                f"{'.'.join(path[:depth]) or '(root)'}; refusing to mutate",
            )
        if not matches:
            if require:
                raise _path_issue(
                    path[: depth + 1],
                    "PATH_NOT_FOUND",
                    f"section {dotted!r} not found in config",
                )
            return None, 0, 0
        header_idx = matches[0]
        if lines[header_idx].rest.strip():
            raise _path_issue(
                path[: depth + 1],
                "INLINE_UNSUPPORTED_SHAPE",
                f"section {dotted!r} uses inline shape; refusing to mutate "
                "(only block-style mappings are supported)",
            )
        parent_idx = header_idx
        block_start = header_idx + 1
        block_end = _block_end_index(lines, header_idx)
    return parent_idx, block_start, block_end


def _find_direct_child(
    lines: list[_YamlLine],
    block_start: int,
    block_end: int,
    parent_indent: int | None,
    field: str,
    path: tuple[str, ...],
) -> int:
    expected_indent = 0 if parent_indent is None else parent_indent + 2
    matches = [
        idx for idx in range(block_start, block_end)
        if lines[idx].key == field and lines[idx].indent == expected_indent
    ]
    if len(matches) > 1:
        raise _path_issue(
            path,
            "AMBIGUOUS_LEAF",
            f"multiple direct leaves named {field!r} under "
            f"{'.'.join(path[:-1]) or '(root)'}; refusing to mutate",
        )
    return matches[0] if matches else -1


def _find_scalar_line(
    lines: list[_YamlLine], parent_path: tuple[str, ...], field: str
) -> int:
    """Find a scalar only inside the exact resolved parent block."""
    parent_idx, block_start, block_end = _resolve_block(
        lines, parent_path, require=False
    )
    if parent_path and parent_idx is None:
        return -1
    parent_indent = None if parent_idx is None else lines[parent_idx].indent
    return _find_direct_child(
        lines, block_start, block_end, parent_indent, field,
        parent_path + (field,),
    )


def _ensure_section_lines(
    lines: list[_YamlLine],
    parent_path: tuple[str, ...],
) -> None:
    """Create missing sections beneath the exact resolved parent only."""
    for depth in range(len(parent_path)):
        current = parent_path[: depth + 1]
        existing, _, _ = _resolve_block(lines, current, require=False)
        if existing is not None:
            continue
        container_path = parent_path[:depth]
        container, _, container_end = _resolve_block(
            lines, container_path, require=True
        )
        if container is None:
            insert_at = len(lines)
            indent = 0
        else:
            insert_at = container_end
            indent = lines[container].indent + 2
        key = parent_path[depth]
        lines.insert(insert_at, _YamlLine(
            raw=(" " * indent) + key + ":",
            indent=indent,
            key=key,
            rest="",
        ))


def _mutate_scalar(
    text: str,
    parent_path: tuple[str, ...],
    field: str,
    value: Any,
) -> str:
    """Replace or insert one scalar under its exact parent block."""
    canonical_field = _resolve_field_alias(parent_path, field)
    lines = _tokenise_yaml_lines(text)
    _ensure_section_lines(lines, parent_path)
    idx = _find_scalar_line(lines, parent_path, canonical_field)
    if idx == -1:
        parent_idx, _, parent_end = _resolve_block(
            lines, parent_path, require=True
        )
        indent = 0 if parent_idx is None else lines[parent_idx].indent + 2
        insert_at = parent_end
        scalar = _safe_yaml_scalar(value)
        lines.insert(insert_at, _YamlLine(
            raw=(" " * indent) + canonical_field + ": " + scalar,
            indent=indent,
            key=canonical_field,
            rest=" " + scalar,
        ))
        idx = _find_scalar_line(lines, parent_path, canonical_field)
    if idx < 0:
        raise _path_issue(
            parent_path + (canonical_field,),
            "TARGET_NOT_FOUND",
            "target was not found after section insertion",
        )
    line = lines[idx]
    new_rest, _ = _scalar_replacement_lines(line.rest, value)
    needs_space = bool(new_rest) and not new_rest.startswith((" ", "\t"))
    separator = " " if needs_space else ""
    raw = (" " * line.indent) + line.key + ":" + separator + new_rest
    lines[idx] = _YamlLine(
        raw=raw, indent=line.indent, key=line.key, rest=new_rest
    )
    separator = "\r\n" if "\r\n" in text else "\n"
    output = separator.join(line.raw for line in lines)
    if text.endswith("\r\n"):
        output += "\r\n"
    elif text.endswith("\n"):
        output += "\n"
    return output


# ---------------------------------------------------------------------------
# ConfigControlService
# ---------------------------------------------------------------------------

@dataclass
class _SecretField:
    """Marker for fields whose value must never appear in show()."""

    name: str


_SECRET_FIELDS = {
    "apiKey", "api_key", "password", "token", "dsn",
}


def _is_secret_key(key: str) -> bool:
    return key in _SECRET_FIELDS


@dataclass
class ConfigControlService:
    """Reusable config control service bound to one profile/path.

    Constructor resolves the target path eagerly (without reading the file).
    All read paths share the same resolution logic as the legacy ``validate``
    helper, so a service is interchangeable with a one-shot validate call.
    """

    profile: str = "default"
    env: Mapping[str, str] | None = None
    path: Path | None = None
    _path_resolution_error: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            return
        resolved, source_or_err = _resolve_config_path(self.profile, self.env)
        if resolved is None:
            # Preserve the resolver's real error so ``read_data`` can report
            # the actual reason (e.g. ``V3CORE_CONFIG must be an absolute
            # path``) instead of falling through to a misleading
            # CONFIG_NOT_FOUND on the fallback home path. ``self.path`` is
            # still set to the conventional default location purely for
            # reporting/display; the stored error short-circuits any I/O.
            self._path_resolution_error = source_or_err or \
                "could not resolve config path"
            resolved = (
                Path.home() / ".v3-core" / "profiles" / self.profile
                / "config.yaml"
            )
        self.path = resolved

    # ----- read -----

    def read_text(self) -> str:
        return _read_text(self.path)

    def read_data(self) -> tuple[Any, list[ConfigIssue]]:
        """Read + parse + structural-check the config.

        Returns ``(data, issues)``. ``data`` may be ``None`` if YAML parsing
        failed; ``issues`` always contains the full diagnostic list.
        """
        if self._path_resolution_error is not None:
            # The resolver refused to pick one — surface the real reason
            # instead of attempting to read the conventional fallback path
            # (which would never exist and produce a misleading
            # CONFIG_NOT_FOUND). The error message is the safe one returned
            # by ``_resolve_config_path``; it never echoes raw input or
            # secret material.
            return None, [ConfigIssue(
                field="",
                severity="error",
                code="CONFIG_PATH_ERROR",
                message=self._path_resolution_error,
            )]
        try:
            text = self.read_text()
        except FileNotFoundError:
            return None, [ConfigIssue(
                field="",
                severity="error",
                code="CONFIG_NOT_FOUND",
                message=f"config file does not exist: {self.path}",
            )]
        except UnicodeDecodeError as e:
            return None, [ConfigIssue(
                field="_raw",
                severity="error",
                code="BAD_ENCODING",
                message=f"file is not valid UTF-8: {e.reason} at byte {e.start}",
                byte_offset=e.start,
            )]
        except OSError as e:
            return None, [ConfigIssue(
                field="",
                severity="error",
                code="READ_ERROR",
                message=f"could not read config file: {e}",
            )]
        issues: list[ConfigIssue] = []
        data, parse_issue = _parse_yaml(text)
        if parse_issue is not None:
            issues.append(parse_issue)
            # Even when YAML failed, scan the raw text for control chars so
            # the diagnostic field can be labelled (e.g. storage.embed.apiKey)
            # rather than just the bare ``_raw`` sentinel. Mirrors the
            # parse-failure branch in ``_validate_candidate_text``; the raw
            # scanner never echoes the offending bytes, so secrets cannot
            # leak through this path.
            issues.extend(_scan_control_chars(text))
            return data, issues
        issues.extend(_validate_structure(data))
        issues.extend(_all_control_char_issues(text, data))
        return data, issues

    # ----- validate / assert_valid -----

    def validate(self) -> list[ConfigIssue]:
        _, issues = self.read_data()
        return issues

    def assert_valid(self) -> None:
        issues = self.validate()
        if issues:
            raise ConfigValidationError(issues)

    # ----- show (redacted) -----

    def show(self, redact: bool = True, as_json: bool = False) -> str:
        """Return a redacted summary of the current config.

        Never echoes secret values. Never opens the network or calls
        ``resolve_config``. Output is either human-readable text or JSON.
        """
        data, issues = self.read_data()
        report = self._build_show_report(data, issues, redact=redact)
        if as_json:
            return _show_as_json(report)
        return _show_as_text(report, self.profile, self.path, redact=redact)

    def _build_show_report(
        self, data: Any, issues: list[ConfigIssue], *, redact: bool
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "profile": self.profile,
            "path": str(self.path),
            "issues": [str(i) for i in issues],
            "db": {},
            "providers": {},
            "toggles": {},
            "readiness": "unknown",
        }
        if not isinstance(data, Mapping):
            report["readiness"] = "unparseable"
            return report

        storage = data.get("storage") if isinstance(data.get("storage"),
                                                     Mapping) else {}
        pg = storage.get("pg") if isinstance(storage, Mapping) and \
            isinstance(storage.get("pg"), Mapping) else {}
        db_section = report["db"]  # type: dict[str, Any]
        if pg:
            for k in ("host", "port", "database", "user"):
                if k in pg:
                    db_section[k] = pg.get(k)
        else:
            db_section["present"] = False
        # Password status only — never the value. We count the raw in-config
        # ``password`` field and the ``V3CORE_PG_PASSWORD`` env fallback as
        # configuration, so operators see whether the deployment has any
        # usable PG credential without ever echoing it. The env source
        # used here is authoritative: when the service was constructed
        # with an explicit ``env=...`` mapping (e.g. the lab/test
        # surface), ``self.env`` is consulted in preference to the
        # process ``os.environ``. Only when ``self.env is None`` do we
        # fall back to the process environment. ``self.env`` is never
        # mutated as a side effect.
        raw_pw = pg.get("password") if isinstance(pg, Mapping) else None
        env_source = self.env if self.env is not None else os.environ
        env_pw = env_source.get("V3CORE_PG_PASSWORD", "")
        pw_configured = (
            (isinstance(raw_pw, str) and bool(raw_pw.strip()))
            or bool(env_pw.strip())
        )
        db_section["password"] = "CONFIGURED" if pw_configured else "NOT CONFIGURED"

        # Provider + LLM endpoint/model/key.
        llm = data.get("llm") if isinstance(data.get("llm"), Mapping) else {}
        providers = report["providers"]  # type: dict[str, Any]
        for section_name in ("embed", "rerank"):
            section = storage.get(section_name) if isinstance(storage,
                                                              Mapping) else {}
            if not isinstance(section, Mapping):
                section = {}
            providers[section_name] = self._section_status(section,
                                                           redact=redact)
        providers["llm"] = self._section_status(llm, redact=redact)

        # Toggles.
        toggles = report["toggles"]  # type: dict[str, Any]
        e1 = data.get("e1") if isinstance(data.get("e1"), Mapping) else {}
        tkg = data.get("tkg") if isinstance(data.get("tkg"), Mapping) else {}
        prefetch = data.get("prefetch") if isinstance(data.get("prefetch"),
                                                      Mapping) else {}
        if isinstance(e1, Mapping) and "enabled" in e1:
            toggles["e1.enabled"] = bool(e1.get("enabled"))
        if isinstance(tkg, Mapping) and "enabled" in tkg:
            toggles["tkg.enabled"] = bool(tkg.get("enabled"))
        if isinstance(prefetch, Mapping):
            for k in ("enabled", "dual_path", "include_message_vector"):
                if k in prefetch:
                    toggles[f"prefetch.{k}"] = bool(prefetch.get(k))

        # Readiness (conservative): only checks shape, never network.
        report["readiness"] = self._readiness(data, issues)
        return report

    @staticmethod
    def _section_status(section: Mapping[str, Any], *, redact: bool
                        ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        endpoint = section.get("endpoint")
        # LLM runtime exposes its endpoint as ``base_url`` (no ``endpoint``
        # scalar). For display purposes, when ``endpoint`` is absent we
        # fall back to ``base_url`` so a configured LLM does not falsely
        # report ``endpoint: NOT CONFIGURED``. Embedding / rerank keep the
        # ``endpoint``-only contract.
        base_url = section.get("base_url")
        endpoint_source: str | None = None
        if isinstance(endpoint, str) and endpoint.strip():
            endpoint_source = endpoint
        elif isinstance(base_url, str) and base_url.strip():
            endpoint_source = base_url
        model = section.get("model")
        # Accept apiKey or api_key as the secret slot.
        secret_key = "apiKey" if "apiKey" in section else (
            "api_key" if "api_key" in section else None
        )
        out["endpoint"] = ("CONFIGURED"
                           if endpoint_source is not None
                           else "NOT CONFIGURED")
        out["model"] = ("CONFIGURED"
                        if isinstance(model, str) and model.strip()
                        else "NOT CONFIGURED")
        if secret_key is not None:
            raw = section.get(secret_key)
            configured = isinstance(raw, str) and bool(raw.strip())
            out["key"] = "CONFIGURED" if configured else "NOT CONFIGURED"
        else:
            out["key"] = "NOT CONFIGURED"
        if not redact:
            # Caller asked explicitly for non-redacted view (still excludes
            # any *secret*-labelled fields — show() never echoes secrets).
            # Endpoint is passed through the safe URL formatter so query,
            # fragment, and userinfo credentials are never echoed in show()
            # output, even at redact=False. Model values are kept verbatim
            # since they carry no secrets.
            if endpoint_source is not None:
                out["endpoint_value"] = _safe_endpoint_url(endpoint_source)
            if isinstance(model, str):
                out["model_value"] = model
        return out

    @staticmethod
    def _safe_endpoint_url(endpoint: str) -> str:
        """Redact an endpoint URL for ``show(redact=False)`` output.

        Implemented as a thin wrapper so the public formatter stays a
        module-level helper while still being reachable via the class.
        """
        return _safe_endpoint_url(endpoint)

    @staticmethod
    def _existing_secret_alias(text: str, parent: tuple[str, ...]) -> str:
        """Return the existing api-key spelling in the exact parent block."""
        lines = _tokenise_yaml_lines(text)
        parent_idx, block_start, block_end = _resolve_block(
            lines, parent, require=False
        )
        if parent and parent_idx is None:
            return "apiKey"
        parent_indent = None if parent_idx is None else lines[parent_idx].indent
        if _find_direct_child(
            lines, block_start, block_end, parent_indent, "apiKey",
            parent + ("apiKey",),
        ) >= 0:
            return "apiKey"
        if _find_direct_child(
            lines, block_start, block_end, parent_indent, "api_key",
            parent + ("api_key",),
        ) >= 0:
            return "api_key"
        return "apiKey"

    @staticmethod
    def _readiness(data: Any, issues: list[ConfigIssue]) -> str:
        """Conservative readiness — does not touch the network.

        Returns one of: ``"ok"``, ``"config_invalid"``, ``"missing_required"``,
        ``"incomplete"``, ``"unknown"``.
        """
        if any(i.severity == "error" for i in issues):
            return "config_invalid"
        if not isinstance(data, Mapping):
            return "config_invalid"
        storage = data.get("storage")
        if not isinstance(storage, Mapping):
            return "missing_required"
        pg = storage.get("pg") if isinstance(storage, Mapping) else None
        if not isinstance(pg, Mapping):
            return "missing_required"
        for must in ("host", "port", "database", "user"):
            if must not in pg:
                return "missing_required"
        # LLM/embed are optional in the conservative view; flag incomplete.
        embed = storage.get("embed")
        llm = data.get("llm")
        any_partial = False
        if embed is None:
            any_partial = True
        if llm is None:
            any_partial = True
        return "incomplete" if any_partial else "ok"

    # ----- atomic update -----

    def atomic_update(
        self,
        mutator: Callable[[str], str],
        *,
        expected: Callable[
            [str], list[tuple[tuple[str, ...], Any]]
        ] | None = None,
    ) -> None:
        """Read text → mutate → parse+validate → flush+fsync temp →
        re-validate temp → ``os.replace`` to final path.

        The mutator receives the current file text and returns the new text.
        On any failure the original file is left byte-for-byte unchanged.
        No ``.bak`` / ``.old`` / history files are created.
        """
        # H1 guard: if path resolution already failed (e.g. V3CORE_CONFIG is
        # relative), refuse to fall through to ``read_text`` on the conventional
        # fallback home path. Without this short-circuit, atomic_update would
        # either raise a misleading CONFIG_NOT_FOUND on the fallback path or,
        # worse, mutate the real ``~/.v3-core/profiles/<profile>/config.yaml``
        # when that file happens to exist. Mirror the resolution-error
        # surface used by ``read_data`` (code CONFIG_PATH_ERROR, safe message)
        # so callers see the real resolver reason instead of a misleading
        # I/O failure. ``self.path`` is intentionally not consulted here — the
        # error short-circuits before any disk touch.
        if getattr(self, "_path_resolution_error", None):
            raise ConfigValidationError([ConfigIssue(
                field="config_path",
                severity="error",
                code="CONFIG_PATH_ERROR",
                message=self._path_resolution_error,
            )])
        if mutator is None or not callable(mutator):
            raise ConfigValidationError([ConfigIssue(
                field="mutator",
                severity="error",
                code="BAD_MUTATOR",
                message="mutator must be callable",
            )])
        try:
            original = self.read_text()
        except FileNotFoundError:
            raise ConfigValidationError([ConfigIssue(
                field="",
                severity="error",
                code="CONFIG_NOT_FOUND",
                message=f"config file does not exist: {self.path}",
            )])

        # 1. Compute candidate.
        try:
            candidate = mutator(original)
        except ConfigValidationError:
            raise
        except Exception as e:
            raise ConfigValidationError([ConfigIssue(
                field="mutator",
                severity="error",
                code="MUTATOR_RAISED",
                message=f"mutator raised {type(e).__name__}",
            )]) from None

        if not isinstance(candidate, str):
            raise ConfigValidationError([ConfigIssue(
                field="mutator",
                severity="error",
                code="MUTATOR_BAD_RETURN",
                message=(
                    "mutator must return str, got "
                    f"{type(candidate).__name__}"
                ),
            )])

        # 2. Validate candidate before touching disk.
        cand_issues = _validate_candidate_text(candidate)
        if cand_issues:
            raise ConfigValidationError(cand_issues)
        if expected is not None:
            try:
                expected_targets = expected(original)
            except ConfigValidationError:
                raise
            except Exception:
                raise ConfigValidationError([ConfigIssue(
                    field="expected",
                    severity="error",
                    code="EXPECTED_CALLBACK_RAISED",
                    message="semantic expectation callback failed",
                )]) from None
            semantic_issues = _validate_expected_targets(
                candidate, expected_targets
            )
            if semantic_issues:
                raise ConfigValidationError(semantic_issues)

        # 3. Write to same-directory temp + fsync.
        target_dir = self.path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".config_control.", suffix=".tmp",
            dir=str(target_dir),
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(candidate)
                f.flush()
                os.fsync(f.fileno())

            # 4. Re-validate the freshly flushed temp.
            tmp_issues = _validate_path_text(tmp_path)
            if tmp_issues:
                raise ConfigValidationError(tmp_issues)

            # 5. Atomic replace.
            os.replace(tmp_path, self.path)
            # Best-effort fsync of the directory so the rename is durable.
            # Guarded: os.O_DIRECTORY is POSIX-only (missing on Windows Python),
            # and some filesystems reject directory fsyncs. Both must be silently
            # skipped without affecting the already-succeeded os.replace above.
            _o_directory = getattr(os, "O_DIRECTORY", 0)
            if _o_directory:
                try:
                    dir_fd = os.open(str(target_dir),
                                     _o_directory | os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    # Non-fatal: directory fsync unsupported on this platform/FS.
                    pass
        finally:
            # 6. Always clean up the temp file if it's still on disk.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    # ----- high-level mutators (all call atomic_update) -----

    def set_provider(
        self,
        target: str,
        endpoint: str | None = None,
        model: str | None = None,
    ) -> None:
        """Set ``endpoint`` and/or ``model`` on ``target``.

        ``target`` is one of ``embedding`` / ``rerank`` / ``llm`` (legacy
        aliases map to ``storage.embed`` / ``storage.rerank`` / ``llm``).

        Either ``endpoint`` or ``model`` may be ``None`` (skip that field);
        at least one must be a non-empty string after trim. When only one
        is provided, the other is left untouched on disk (no insertion, no
        removal, no comment rewrite). When updating a field that already
        exists, any inline trailing comment on that line is preserved.
        """
        if endpoint is None and model is None:
            raise ConfigValidationError([ConfigIssue(
                field="endpoint",
                severity="error",
                code="EMPTY_VALUE",
                message=(
                    "at least one of endpoint or model must be a non-empty "
                    "string"
                ),
            )])
        # Validate type / non-empty hygiene on whatever was provided.
        for fld, val in (("endpoint", endpoint), ("model", model)):
            if val is None:
                continue
            if not isinstance(val, str):
                raise ConfigValidationError([ConfigIssue(
                    field=fld,
                    severity="error",
                    code="BAD_TYPE",
                    message=f"{fld} must be a string",
                )])
            if not val.strip():
                raise ConfigValidationError([ConfigIssue(
                    field=fld,
                    severity="error",
                    code="EMPTY_VALUE",
                    message=f"{fld} must be a non-empty string",
                )])
        # Validate control-char hygiene before mutating (so the error message
        # can mention the offending field but never its content).
        for fld, val in (("endpoint", endpoint), ("model", model)):
            if val is None:
                continue
            issues = _scan_control_chars_in_value(val, fld)
            if issues:
                raise ConfigValidationError(issues)
        parent = _resolve_target_path(target)
        _reject_inline_unsupported(self, parent)

        # The runtime field name for the LLM endpoint is ``base_url`` (it
        # is not a literal ``endpoint`` scalar). For embedding / rerank
        # we keep the existing ``endpoint`` field. Either-or-only model
        # semantics are preserved: passing only endpoint writes only the
        # endpoint slot, and the same for model.
        endpoint_field = "endpoint"
        if target == "llm":
            endpoint_field = "base_url"

        def _mutate(text: str) -> str:
            new = text
            if endpoint is not None:
                new = _mutate_scalar(new, parent, endpoint_field, endpoint)
            if model is not None:
                new = _mutate_scalar(new, parent, "model", model)
            return new

        def _expected(_original: str) -> list[tuple[tuple[str, ...], Any]]:
            targets: list[tuple[tuple[str, ...], Any]] = []
            if endpoint is not None:
                targets.append((parent + (endpoint_field,), endpoint))
            if model is not None:
                targets.append((parent + ("model",), model))
            return targets

        self.atomic_update(_mutate, expected=_expected)

    def set_keys(self, targets: list[str], secret: str) -> None:
        """Set the secret (apiKey/api_key) for each target atomically.

        All-or-none: if any target/secret fails validation, no disk write
        happens. ``secret`` must already be normalised (use
        ``normalize_secret_input`` first). Never includes the secret value in
        any error message.
        """
        if not isinstance(targets, list) or not targets:
            raise ConfigValidationError([ConfigIssue(
                field="targets",
                severity="error",
                code="BAD_TARGETS",
                message="targets must be a non-empty list",
            )])
        # Validate every target up-front.
        resolved_paths: list[tuple[str, ...]] = []
        for t in targets:
            parent = _resolve_target_path(t)
            _reject_inline_unsupported(self, parent)
            resolved_paths.append(parent)
        # Validate secret hygiene (no echo, no embedded control chars).
        if not isinstance(secret, str):
            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="BAD_SECRET_TYPE",
                message="secret must be a string",
            )])
        if not secret.strip():
            raise ConfigValidationError([ConfigIssue(
                field="secret",
                severity="error",
                code="EMPTY_SECRET",
                message="secret must not be empty",
            )])
        for i, ch in enumerate(secret):
            cp = ord(ch)
            if cp < 0x20 or cp == 0x7F:
                raise ConfigValidationError([ConfigIssue(
                    field="secret",
                    severity="error",
                    code="SECRET_CONTROL_CHAR",
                    message=(
                        f"refusing control character U+{cp:04X} in secret"
                    ),
                    byte_offset=i,
                )])

        def _mutate(text: str) -> str:
            new = text
            for parent in resolved_paths:
                field_name = self._existing_secret_alias(new, parent)
                new = _mutate_scalar(new, parent, field_name, secret)
            return new

        def _expected(original: str) -> list[tuple[tuple[str, ...], Any]]:
            targets: list[tuple[tuple[str, ...], Any]] = []
            for parent in resolved_paths:
                field_name = self._existing_secret_alias(original, parent)
                targets.append((parent + (field_name,), secret))
            return targets

        self.atomic_update(_mutate, expected=_expected)

    def set_toggle(self, name: str, value: bool) -> None:
        """Flip one of the supported toggles.

        Supported: ``e1.enabled``, ``tkg.enabled``, ``prefetch.enabled``,
        ``prefetch.dual_path``, ``prefetch.include_message_vector``.
        """
        if not isinstance(value, bool):
            raise ConfigValidationError([ConfigIssue(
                field="value",
                severity="error",
                code="BAD_TYPE",
                message=(
                    "toggle value must be a bool, got "
                    f"{type(value).__name__}"
                ),
            )])
        parent, field = _parse_toggle_path(name)
        _reject_inline_unsupported(self, parent)

        def _mutate(text: str) -> str:
            return _mutate_scalar(text, parent, field, value)

        def _expected(_original: str) -> list[tuple[tuple[str, ...], Any]]:
            return [((parent + (field,)), value)]

        self.atomic_update(_mutate, expected=_expected)


def _reject_inline_unsupported(
    service: ConfigControlService, parent: tuple[str, ...]
) -> None:
    """Resolve the exact parent once, rejecting inline/ambiguous shapes."""
    if service._path_resolution_error:
        raise ConfigValidationError([ConfigIssue(
            field="config_path",
            severity="error",
            code="CONFIG_PATH_ERROR",
            message=service._path_resolution_error,
        )])
    try:
        text = service.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return
    _resolve_block(_tokenise_yaml_lines(text), parent, require=False)


def _parse_toggle_path(name: str) -> tuple[tuple[str, ...], str]:
    parts = name.split(".")
    if len(parts) != 2 or not all(parts):
        raise ConfigValidationError([ConfigIssue(
            field=name,
            severity="error",
            code="UNKNOWN_TOGGLE",
            message=(
                f"unknown toggle {name!r}; supported: e1.enabled, "
                "tkg.enabled, prefetch.enabled, prefetch.dual_path, "
                "prefetch.include_message_vector"
            ),
        )])
    parent = (parts[0],)
    field = parts[1]
    if (parent, field) not in {
        (("e1",), "enabled"),
        (("tkg",), "enabled"),
        (("prefetch",), "enabled"),
        (("prefetch",), "dual_path"),
        (("prefetch",), "include_message_vector"),
    }:
        raise ConfigValidationError([ConfigIssue(
            field=name,
            severity="error",
            code="UNKNOWN_TOGGLE",
            message=(
                f"unknown toggle {name!r}; supported: e1.enabled, "
                "tkg.enabled, prefetch.enabled, prefetch.dual_path, "
                "prefetch.include_message_vector"
            ),
        )])
    return parent, field


# ---------------------------------------------------------------------------
# show() rendering helpers
# ---------------------------------------------------------------------------

def _show_as_text(report: dict[str, Any], profile: str,
                  path: Path, *, redact: bool) -> str:
    out: list[str] = []
    out.append(f"profile: {profile}")
    out.append(f"path:    {path}")
    issues = report.get("issues") or []
    if issues:
        out.append("issues:")
        for s in issues:
            out.append(f"  - {s}")
    else:
        out.append("issues:  none")
    out.append("db:")
    db = report.get("db") or {}
    if not db:
        out.append("  (missing)")
    else:
        for k in ("host", "port", "database", "user", "password"):
            if k in db:
                out.append(f"  {k}: {db[k]}")
    out.append("providers:")
    providers = report.get("providers") or {}
    for name in ("embed", "rerank", "llm"):
        sect = providers.get(name) or {}
        out.append(f"  {name}:")
        for k in ("endpoint", "model", "key"):
            if k in sect:
                out.append(f"    {k}: {sect[k]}")
        if not redact:
            for k in ("endpoint_value", "model_value"):
                if k in sect:
                    out.append(f"    {k}: {sect[k]}")
    out.append("toggles:")
    toggles = report.get("toggles") or {}
    if not toggles:
        out.append("  (none reported)")
    else:
        for k, v in toggles.items():
            out.append(f"  {k}: {v}")
    out.append(f"readiness: {report.get('readiness', 'unknown')}")
    return "\n".join(out)


def _show_as_json(report: dict[str, Any]) -> str:
    import json
    return json.dumps(report, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Candidate validation helpers used by atomic_update
# ---------------------------------------------------------------------------

_SEMANTIC_MISSING = object()


def _parsed_path_value(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return _SEMANTIC_MISSING
        current = current[part]
    return current


def _strict_value_equal(actual: Any, intended: Any) -> bool:
    if isinstance(intended, bool):
        return isinstance(actual, bool) and actual == intended
    if isinstance(intended, str):
        return isinstance(actual, str) and actual == intended
    return type(actual) is type(intended) and actual == intended


def _validate_expected_targets(
    candidate: str,
    expected: list[tuple[tuple[str, ...], Any]],
) -> list[ConfigIssue]:
    """Validate exact mutation targets after candidate YAML parsing."""
    parsed, parse_issue = _parse_yaml(candidate)
    if parse_issue is not None:
        return [parse_issue]
    lines = _tokenise_yaml_lines(candidate)
    issues: list[ConfigIssue] = []
    for path, intended in expected:
        dotted = ".".join(path)
        try:
            line_idx = _find_scalar_line(lines, path[:-1], path[-1])
        except ConfigValidationError as exc:
            issues.extend(exc.issues)
            continue
        if line_idx < 0:
            issues.append(ConfigIssue(
                field=dotted,
                severity="error",
                code="EXPECTED_TARGET_MISSING",
                message="mutation target is missing after serialization",
            ))
            continue
        actual = _parsed_path_value(parsed, path)
        if actual is _SEMANTIC_MISSING:
            issues.append(ConfigIssue(
                field=dotted,
                severity="error",
                code="EXPECTED_TARGET_MISSING",
                message="parsed mutation target is missing after serialization",
            ))
            continue
        if not _strict_value_equal(actual, intended):
            issues.append(ConfigIssue(
                field=dotted,
                severity="error",
                code="EXPECTED_VALUE_MISMATCH",
                message="parsed mutation value differs from intended value",
            ))
    return issues


def _validate_candidate_text(text: str) -> list[ConfigIssue]:
    """Strict validate used before flush: C0/DEL + parse + structure.

    Returns the full list; empty means OK.
    """
    issues: list[ConfigIssue] = []
    data, parse_issue = _parse_yaml(text)
    if parse_issue is not None:
        issues.append(parse_issue)
        # Still scan for control chars even when YAML failed.
        issues.extend(_scan_control_chars(text))
        return issues
    issues.extend(_all_control_char_issues(text, data))
    issues.extend(_validate_structure(data))
    return issues


def _validate_path_text(path: Path) -> list[ConfigIssue]:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as e:
        return [ConfigIssue(
            field="_raw",
            severity="error",
            code="BAD_ENCODING",
            message=f"file is not valid UTF-8: {e.reason} at byte {e.start}",
            byte_offset=e.start,
        )]
    except OSError as e:
        return [ConfigIssue(
            field="",
            severity="error",
            code="READ_ERROR",
            message=f"could not read temp file: {e}",
        )]
    return _validate_candidate_text(text)


# ---------------------------------------------------------------------------
# Explicit provider-test surface (no CLI; called only by test_target())
#
# These methods are intentionally separated from ``show()`` / ``validate()``
# / mutation paths: they are NEVER invoked on import or as a side-effect of
# reading or writing the config. Each call performs a single live round-trip
# to the configured provider using one synthetic input, and never returns
# api keys, Bearer headers, raw vectors, or other secret-shaped values.
# ---------------------------------------------------------------------------

# Targets accepted by ``test_target``. Aliases mirror ``_KNOWN_TARGETS`` plus
# ``postgres`` so a CLI can dispatch by friendly name without exposing the
# raw section names.
_TEST_TARGET_ALIASES: dict[str, str] = {
    "embedding": "embedding",
    "embed": "embedding",
    "rerank": "rerank",
    "llm": "llm",
    "postgres": "postgres",
    "pg": "postgres",
}


def _safe_test_error(exc: BaseException) -> str:
    """Sanitize a provider-test exception into a single-line, secret-free string.

    Never echoes:
      * ``Bearer ...`` headers
      * DSN userinfo (``user:pass@host``)
      * ``password=...`` / ``pwd=...`` / ``passwd=...`` values
      * ``apiKey=...`` / ``api_key=...`` values
      * vector arrays (they live inside repr() of long lists)

    The output is also collapsed to ASCII-printable single-line and capped
    so it can be embedded in a JSON-safe dict without leaking surrounding
    context.
    """
    try:
        from . import _safe_err  # type: ignore
        msg = _safe_err(exc, 200)
    except Exception:
        msg = repr(exc)
    # Belt-and-braces: _safe_err already scrubs the four signatures, but a
    # caller may surface the raw ``str(exc)`` path. Re-run the same regexes
    # so the public contract is independent of the v3core top-level import.
    try:
        s = str(msg)[:400]
        s = re.sub(r"(?i)(api[_-]?key)\s*[=:]\s*\S+", r"\1=***", s)
        s = re.sub(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+",
                   r"\1=***", s)
        s = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", s)
        s = re.sub(r"://[^:\s]+:[^@\s]+@", "://***:***@", s)
        # Drop any obvious long vector / JSON arrays from repr().
        s = re.sub(r"\[[^\[\]]{120,}\]", "[...]", s)
        # Collapse to a single line, trim, cap length.
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 200:
            s = s[:197] + "..."
        return s or type(exc).__name__
    except Exception:
        return type(exc).__name__


def _safe_endpoint_for_test(endpoint: Any) -> str:
    """Public-format an endpoint URL for test reports without leaking secrets."""
    if not isinstance(endpoint, str):
        return ""
    return _safe_endpoint_url(endpoint)


def _resolve_runtime_cfg(self) -> tuple[Any, list[ConfigIssue]]:
    """Load + offline-validate the resolved profile via ``v3core.config``.

    Returns ``(cfg, issues)``. ``cfg`` is the typed ``V3Config`` from
    ``resolve_config``; ``issues`` is the offline-validation issue list
    (empty when valid). Never raises for offline problems — those are
    reported as ``issues``. A real ``RuntimeError`` (e.g. missing
    ``V3CORE_PG_PASSWORD``) is also captured into ``issues`` rather than
    being re-raised, so the test surface stays exception-safe.

    When the service was constructed with an explicit ``path=...``
    (the provider-test surface always passes the lab path), we
    temporarily bind ``os.environ['V3CORE_CONFIG']`` to that absolute
    path for the duration of the ``resolve_config`` call so the typed
    loader never falls back to the user's home / production config.
    The original environment value is restored exactly in ``finally``,
    no backup file is created, and no network call is made.
    """
    profile = self.profile or "default"
    # First — local file hygiene via ``validate``. Cheap, no I/O beyond
    # the file we already resolved.
    local_issues = self.validate()
    fatal = [i for i in local_issues if i.severity == "error"]
    if fatal:
        return None, local_issues
    # Second — formal ``resolve_config`` (typed). Catch import / env errors
    # so a missing dep or env var does not propagate to the test surface.
    try:
        from v3core.config import resolve_config as _resolve_config
    except Exception as e:  # pragma: no cover - exercised only when v3core unimportable
        return None, local_issues + [ConfigIssue(
            field="resolve_config",
            severity="error",
            code="RESOLVE_IMPORT_FAILED",
            message=f"v3core.config.resolve_config unavailable: {_safe_test_error(e)}",
        )]

    # Provider-test surface always constructs the service with an explicit
    # absolute ``path``. Pin ``V3CORE_CONFIG`` to that path so the typed
    # loader reads the lab file rather than the user's home / production
    # config. Capture the exact prior state so ``finally`` can restore it
    # byte-for-byte — even if ``V3CORE_CONFIG`` was unset or held an
    # unrelated value, and even if ``_resolve_config`` raises.
    #
    # ``self.env`` (when set on the service) is also overlaid: for every
    # known lab-injected key we temporarily bind the process environment
    # to that exact value, and restore the prior value (or pop the key
    # if it was absent) in ``finally``. This keeps ``resolve_config`` in
    # the lab test blind to the host process environment and prevents a
    # custom ``env={}`` from accidentally inheriting a production
    # ``V3CORE_PG_PASSWORD``. Explicit ``path`` binding for
    # ``V3CORE_CONFIG`` always wins over any ``self.env["V3CORE_CONFIG"]``
    # entry — the lab surface intends to drive the typed loader at that
    # exact file.
    _env_key = "V3CORE_CONFIG"
    _had_key = _env_key in os.environ
    _prev_value = os.environ.get(_env_key)
    _bound = False
    # Overlay keys driven by ``self.env`` (when the caller supplied an
    # explicit env mapping). Each key is restored exactly in ``finally``
    # so the host process environment never leaks the lab values and
    # never loses any pre-existing value.
    _overlay_specs: list[tuple[str, str | None]] = []
    # Re-entrancy guard: the overlay must not undo the explicit ``path``
    # binding below. If ``self.env`` happens to carry ``V3CORE_CONFIG``
    # we drop it from the overlay (path binding remains authoritative).
    overlay_keys: tuple[str, ...] = ("V3CORE_PG_PASSWORD",)
    if self.env is not None:
        for _k in overlay_keys:
            if _k in self.env:
                _overlay_specs.append((_k, self.env[_k]))
            else:
                # Caller explicitly opted into a custom env. A known key
                # absent from that mapping means "do not inherit the
                # host process value" — so we temporarily remove it
                # from ``os.environ`` rather than letting the host's
                # value sneak through ``resolve_config``.
                _overlay_specs.append((_k, None))
    _overlay_prior: list[tuple[str, bool, str | None]] = []
    try:
        # Apply the lab env overlay first so any error in the overlay
        # itself is still covered by the finally block.
        for _k, _v in _overlay_specs:
            _overlay_prior.append((_k, _k in os.environ, os.environ.get(_k)))
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        if self.path is not None:
            os.environ[_env_key] = str(self.path)
            _bound = True
        try:
            cfg = _resolve_config(profile, hermes_home="")
        except Exception as e:
            return None, local_issues + [ConfigIssue(
                field="resolve_config",
                severity="error",
                code="RESOLVE_RUNTIME_FAILED",
                message=_safe_test_error(e),
            )]
        return cfg, local_issues
    finally:
        # Precise restore — never leak the lab path or lab env values
        # into the caller's environment. If the key was absent before,
        # pop it; if it had a value, restore it verbatim. Skip the
        # round-trip when we never bound the key in the first place
        # (default ``resolve_config`` path or import-failure
        # short-circuit).
        if _bound:
            if _had_key:
                os.environ[_env_key] = _prev_value  # type: ignore[assignment]
            else:
                os.environ.pop(_env_key, None)
        # Restore the lab env overlay in reverse application order.
        for _k, _had, _prev in reversed(_overlay_prior):
            if _had:
                os.environ[_k] = _prev  # type: ignore[assignment]
            else:
                os.environ.pop(_k, None)


def test_embedding(self) -> dict[str, Any]:
    """Run a single ``call_embedding`` against the configured embed endpoint.

    Asserts offline config validity first, then loads the profile through
    ``resolve_config``, builds ``embed_cfg`` via the formal ``build_embed_cfg``
    factory, and issues one ``call_embedding`` with synthetic text
    ``"Hippocampus provider validation"``. Never writes to DB, never retries,
    never raises.

    Returns a safe dict:

        {
            "target": "embedding",
            "ok": bool,
            "endpoint": <safe display URL>,
            "model": <str>,
            "dim": <int|None>,
            "error": <str|None>,
        }

    Never returns the API key, the raw vector, or any Bearer header.
    """
    result: dict[str, Any] = {
        "target": "embedding",
        "ok": False,
        "endpoint": "",
        "model": "",
        "dim": None,
        "error": None,
    }
    cfg, issues = _resolve_runtime_cfg(self)
    if cfg is None:
        result["error"] = _format_issues_brief(issues) or "offline validation failed"
        return result
    embed_section = getattr(cfg, "embed", None)
    endpoint = getattr(embed_section, "endpoint", "") if embed_section else ""
    model = getattr(embed_section, "model", "") if embed_section else ""
    result["endpoint"] = _safe_endpoint_for_test(endpoint)
    result["model"] = model or ""
    if not endpoint or not model:
        result["error"] = "embedding not configured (endpoint or model missing)"
        return result
    # Build the formal embed_cfg and call once. Wrap each step so the dict
    # is always returned, never raised.
    try:
        from v3core.embedding import (
            build_embed_cfg as _build_embed_cfg,
            call_embedding as _call_embedding,
        )
    except Exception as e:  # pragma: no cover
        result["error"] = f"embedding import failed: {_safe_test_error(e)}"
        return result
    try:
        embed_cfg = _build_embed_cfg(cfg)
    except Exception as e:
        result["error"] = f"build_embed_cfg failed: {_safe_test_error(e)}"
        return result
    try:
        # cache=False to avoid contaminating the global cache with a probe
        # call; timeout 3s; retries 0 — one shot, no retry storm.
        vec = _call_embedding(
            "Hippocampus provider validation",
            embed_cfg,
            cache=False,
            timeout=3.0,
            retries=0,
        )
    except Exception as e:
        result["error"] = f"call_embedding failed: {_safe_test_error(e)}"
        return result
    if not isinstance(vec, (list, tuple)) or not vec:
        result["error"] = "call_embedding returned empty vector"
        return result
    try:
        result["dim"] = len(vec)
    except Exception:
        result["dim"] = None
    result["ok"] = True
    return result


def test_rerank(self) -> dict[str, Any]:
    """Run a single ``rerank`` against the configured rerank endpoint.

    Uses query ``"persistent memory"`` and two synthetic documents. Requires
    a non-empty ranked result. Never raises, never returns the api key.

    Returns a safe dict:

        {
            "target": "rerank",
            "ok": bool,
            "endpoint": <safe display URL>,
            "model": <str>,
            "result_count": <int>,
            "error": <str|None>,
        }
    """
    result: dict[str, Any] = {
        "target": "rerank",
        "ok": False,
        "endpoint": "",
        "model": "",
        "result_count": 0,
        "error": None,
    }
    cfg, issues = _resolve_runtime_cfg(self)
    if cfg is None:
        result["error"] = _format_issues_brief(issues) or "offline validation failed"
        return result
    rerank_section = getattr(cfg, "rerank", None)
    endpoint = getattr(rerank_section, "endpoint", "") if rerank_section else ""
    model = getattr(rerank_section, "model", "") if rerank_section else ""
    result["endpoint"] = _safe_endpoint_for_test(endpoint)
    result["model"] = model or ""
    if not endpoint:
        result["error"] = "rerank not configured (endpoint missing)"
        return result
    try:
        from v3core.rerank import rerank as _rerank
    except Exception as e:  # pragma: no cover
        result["error"] = f"rerank import failed: {_safe_test_error(e)}"
        return result
    docs = [
        "Persistent memory systems store state across process restarts.",
        "Vector databases enable semantic similarity retrieval at scale.",
    ]
    try:
        scores = _rerank(
            "persistent memory",
            docs,
            rerank_section.to_legacy_dict()
            if rerank_section is not None and hasattr(rerank_section, "to_legacy_dict")
            else None,
        )
    except Exception as e:
        result["error"] = f"rerank failed: {_safe_test_error(e)}"
        return result
    if not isinstance(scores, (list, tuple)) or not scores:
        result["error"] = "rerank returned empty result"
        return result
    result["result_count"] = len(scores)
    result["ok"] = True
    return result


def test_llm(self) -> dict[str, Any]:
    """Run a single ``LLMClient.chat`` against the configured LLM provider.

    Uses a tiny synthetic prompt with no memory or personal data. Never
    raises, never returns the api key, never returns the response text.
    Reuses the same formal ``LLMClient`` HTTP path as production (no
    parallel HTTP client).

    Returns a safe dict:

        {
            "target": "llm",
            "ok": bool,
            "provider": <str>,
            "model": <str>,
            "endpoint": <safe display URL>,
            "error": <str|None>,
        }
    """
    result: dict[str, Any] = {
        "target": "llm",
        "ok": False,
        "provider": "",
        "model": "",
        "endpoint": "",
        "error": None,
    }
    cfg, issues = _resolve_runtime_cfg(self)
    if cfg is None:
        result["error"] = _format_issues_brief(issues) or "offline validation failed"
        return result
    llm_section = getattr(cfg, "llm", None)
    provider = getattr(llm_section, "provider", "") if llm_section else ""
    model = getattr(llm_section, "model", "") if llm_section else ""
    base_url = getattr(llm_section, "base_url", "") if llm_section else ""
    result["provider"] = provider or ""
    result["model"] = model or ""
    result["endpoint"] = _safe_endpoint_for_test(base_url)
    if not provider or not model:
        result["error"] = "llm not configured (provider or model missing)"
        return result
    try:
        from v3core.llm import LLMClient as _LLMClient
    except Exception as e:  # pragma: no cover
        result["error"] = f"LLMClient import failed: {_safe_test_error(e)}"
        return result
    try:
        client = _LLMClient(cfg)
    except Exception as e:
        result["error"] = f"LLMClient init failed: {_safe_test_error(e)}"
        return result
    if not getattr(client, "api_key", ""):
        result["error"] = "LLM api_key missing"
        return result
    try:
        # Minimal probe — no memory/personal data, low temperature for determinism.
        client.chat(
            system="You are a connectivity probe. Reply with the single word OK.",
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0,
        )
    except Exception as e:
        result["error"] = f"LLMClient.chat failed: {_safe_test_error(e)}"
        return result
    result["ok"] = True
    return result


def test_postgres(self) -> dict[str, Any]:
    """Open a direct psycopg2 connection to the configured PG and run ``SELECT 1``.

    Uses ``cfg.pg`` fields only (host / port / database / user / password).
    Closes cursor + connection on every path. Never runs schema bootstrap,
    migrations, or any write statement. Never raises.

    Returns a safe dict:

        {
            "target": "postgres",
            "ok": bool,
            "host": <str>,
            "port": <int>,
            "database": <str>,
            "user": <str>,
            "error": <str|None>,
        }

    Never returns ``password``.
    """
    result: dict[str, Any] = {
        "target": "postgres",
        "ok": False,
        "host": "",
        "port": None,
        "database": "",
        "user": "",
        "error": None,
    }
    cfg, issues = _resolve_runtime_cfg(self)
    if cfg is None:
        result["error"] = _format_issues_brief(issues) or "offline validation failed"
        return result
    pg = getattr(cfg, "pg", None)
    if pg is None:
        result["error"] = "postgres not configured (cfg.pg is None)"
        return result
    host = getattr(pg, "host", "") or ""
    port = getattr(pg, "port", None)
    database = getattr(pg, "database", "") or ""
    user = getattr(pg, "user", "") or ""
    result["host"] = host
    result["port"] = int(port) if isinstance(port, int) else None
    result["database"] = database
    result["user"] = user
    if not host or not database or not user or not isinstance(port, int):
        result["error"] = "postgres connection fields incomplete"
        return result
    try:
        import psycopg2  # type: ignore
    except Exception as e:  # pragma: no cover
        result["error"] = f"psycopg2 import failed: {_safe_test_error(e)}"
        return result
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(
            host=host,
            port=int(port),
            database=database,
            user=user,
            password=getattr(pg, "password", "") or "",
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        if not row or row[0] != 1:
            result["error"] = "SELECT 1 returned unexpected result"
            return result
    except Exception as e:
        result["error"] = f"postgres SELECT 1 failed: {_safe_test_error(e)}"
        return result
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    result["ok"] = True
    return result


def test_target(self, target: str) -> dict[str, Any]:
    """Dispatch a single provider test by friendly alias.

    Accepts ``embedding`` / ``embed`` / ``rerank`` / ``llm`` /
    ``postgres`` / ``pg``. Unknown targets raise ``ConfigValidationError``
    and are never silently routed to ``show()`` or ``validate()``.
    """
    if not isinstance(target, str):
        raise ConfigValidationError([ConfigIssue(
            field="target",
            severity="error",
            code="BAD_TYPE",
            message=f"target must be a string, got {type(target).__name__}",
        )])
    canonical = _TEST_TARGET_ALIASES.get(target.strip().lower())
    if canonical is None:
        raise ConfigValidationError([ConfigIssue(
            field="target",
            severity="error",
            code="UNKNOWN_TARGET",
            message=(
                f"unknown test target {target!r}; supported: "
                "embedding, rerank, llm, postgres"
            ),
        )])
    if canonical == "embedding":
        return self.test_embedding()
    if canonical == "rerank":
        return self.test_rerank()
    if canonical == "llm":
        return self.test_llm()
    if canonical == "postgres":
        return self.test_postgres()
    # Unreachable — guard against future alias drift.
    raise ConfigValidationError([ConfigIssue(
        field="target",
        severity="error",
        code="UNKNOWN_TARGET",
        message=f"unrouted test target alias {target!r}",
    )])


def _format_issues_brief(issues: list[ConfigIssue]) -> str:
    """Render the first few offline-validation issues as a single line.

    Used by the test_* methods to surface a short reason in ``result.error``.
    Never echoes secret-shaped content — ``ConfigIssue.message`` is the
    already-sanitized form produced by ``validate()``.
    """
    if not issues:
        return ""
    parts = [str(i) for i in issues[:3]]
    out = "; ".join(parts)
    if len(out) > 240:
        out = out[:237] + "..."
    return out


# Attach methods to the existing dataclass without mutating its declaration.
# ``ConfigControlService`` is a frozen-attr-free dataclass so monkey-patching
# the class is safe; this keeps the public method surface co-located with
# its ``__all__`` and avoids a parallel definition.
ConfigControlService.test_embedding = test_embedding  # type: ignore[attr-defined]
ConfigControlService.test_rerank = test_rerank  # type: ignore[attr-defined]
ConfigControlService.test_llm = test_llm  # type: ignore[attr-defined]
ConfigControlService.test_postgres = test_postgres  # type: ignore[attr-defined]
ConfigControlService.test_target = test_target  # type: ignore[attr-defined]


__all__ = [
    "ConfigIssue",
    "ConfigValidationError",
    "ConfigControlService",
    "normalize_secret_input",
    "validate",
]
