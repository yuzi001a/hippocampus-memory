"""v3core.llmstatus — LLM/observer 状态真源（进程内、线程安全、绝不存 key）"""
from __future__ import annotations
import os
import re
import time
import threading
from typing import Any

LLM_ERROR_TYPES = ("credential_missing", "auth_failed", "rate_limited", "timeout", "endpoint_or_model_error", "other")

def _fmt_time(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except Exception:
        return None


def _normalize_error_type(error_type) -> str:
    et = str(error_type) if error_type is not None else "other"
    return et if et in LLM_ERROR_TYPES else "other"


class LLMStatusState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_success_at: float | None = None
        self._last_error_at: float | None = None
        self._last_error_type: str | None = None
        self._observer_last_success_at: float | None = None
        self._observer_last_error_at: float | None = None
        self._observer_last_error_type: str | None = None
        self._observer_last_skip_at: float | None = None
        self._observer_last_skip_reason: str | None = None

    def record_success(self, now: float | None = None) -> None:
        ts = float(now) if now is not None else time.time()
        with self._lock:
            # monotonic: ensure success > previous error if timestamps collide
            if self._last_error_at is not None and ts <= self._last_error_at:
                ts = self._last_error_at + 0.001
            if self._last_success_at is not None and ts <= self._last_success_at:
                ts = self._last_success_at + 0.001
            if self._observer_last_skip_at is not None and ts <= self._observer_last_skip_at:
                ts = self._observer_last_skip_at + 0.001
            self._last_success_at = ts

    def record_error(self, error_type: str | None, now: float | None = None) -> None:
        ts = float(now) if now is not None else time.time()
        et = _normalize_error_type(error_type or "other")
        with self._lock:
            # monotonic over success to preserve ordering when clock resolution low
            if self._last_success_at is not None and ts <= self._last_success_at:
                # error after success should be > success to mark degraded correctly;
                # but if clock hasn't advanced, bump slightly
                ts = self._last_success_at + 0.001
            if self._last_error_at is not None and ts <= self._last_error_at:
                ts = self._last_error_at + 0.001
            self._last_error_at = ts
            self._last_error_type = et

    def record_observer_success(self, now: float | None = None) -> None:
        ts = float(now) if now is not None else time.time()
        with self._lock:
            if self._observer_last_success_at is not None and ts <= self._observer_last_success_at:
                ts = self._observer_last_success_at + 0.001
            if self._observer_last_error_at is not None and ts <= self._observer_last_error_at:
                ts = self._observer_last_error_at + 0.001
            if self._observer_last_skip_at is not None and ts <= self._observer_last_skip_at:
                ts = self._observer_last_skip_at + 0.001
            self._observer_last_success_at = ts

    def record_observer_error(self, error_type: str | None, now: float | None = None) -> None:
        ts = float(now) if now is not None else time.time()
        et = _normalize_error_type(error_type)
        with self._lock:
            if self._observer_last_success_at is not None and ts <= self._observer_last_success_at:
                ts = self._observer_last_success_at + 0.001
            if self._observer_last_error_at is not None and ts <= self._observer_last_error_at:
                ts = self._observer_last_error_at + 0.001
            if self._observer_last_skip_at is not None and ts <= self._observer_last_skip_at:
                ts = self._observer_last_skip_at + 0.001
            self._observer_last_error_at = ts
            self._observer_last_error_type = et

    def record_observer_skip(self, reason: str | None, now: float | None = None) -> None:
        ts = float(now) if now is not None else time.time()
        r = str(reason) if reason is not None else ""
        with self._lock:
            if self._observer_last_success_at is not None and ts <= self._observer_last_success_at:
                ts = self._observer_last_success_at + 0.001
            if self._observer_last_error_at is not None and ts <= self._observer_last_error_at:
                ts = self._observer_last_error_at + 0.001
            if self._observer_last_skip_at is not None and ts <= self._observer_last_skip_at:
                ts = self._observer_last_skip_at + 0.001
            self._observer_last_skip_at = ts
            self._observer_last_skip_reason = r

    def reset(self) -> None:
        with self._lock:
            self._last_success_at = None
            self._last_error_at = None
            self._last_error_type = None
            self._observer_last_success_at = None
            self._observer_last_error_at = None
            self._observer_last_error_type = None
            self._observer_last_skip_at = None
            self._observer_last_skip_reason = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ls = self._last_success_at
            le = self._last_error_at
            let = self._last_error_type
            obs_success = self._observer_last_success_at
            obs_error = self._observer_last_error_at
            obs_error_type = self._observer_last_error_type
            os_at = self._observer_last_skip_at
            os_reason = self._observer_last_skip_reason
        fmt_success = _fmt_time(ls)
        fmt_error = _fmt_time(le)
        fmt_skip = _fmt_time(os_at)
        fmt_obs_success = _fmt_time(obs_success)
        fmt_obs_error = _fmt_time(obs_error)
        # Provide both naming variants for compatibility
        return {
            "last_success_at": fmt_success,
            "last_error_at": fmt_error,
            "last_error_type": let,
            "observer_last_success_at": fmt_obs_success,
            "observer_last_error_at": fmt_obs_error,
            "observer_last_error_type": obs_error_type,
            "observer_last_skip_at": fmt_skip,
            "observer_last_skip_reason": os_reason,
            "last_llm_success_at": fmt_success,
            "last_llm_error_at": fmt_error,
            "last_llm_error_type": let,
        }


# module-level singleton
_state = LLMStatusState()

def record_success(now: float | None = None) -> None:
    _state.record_success(now=now)

def record_error(error_type: str | None, now: float | None = None) -> None:
    _state.record_error(error_type, now=now)

def record_observer_success(now: float | None = None) -> None:
    _state.record_observer_success(now=now)

def record_observer_error(error_type: str | None, now: float | None = None) -> None:
    _state.record_observer_error(error_type, now=now)

def record_observer_skip(reason: str | None, now: float | None = None) -> None:
    _state.record_observer_skip(reason, now=now)

def snapshot() -> dict[str, Any]:
    return _state.snapshot()

def reset() -> None:
    _state.reset()

def _map_status_code(code: int) -> str:
    if code in (401, 403):
        return "auth_failed"
    if code == 429:
        return "rate_limited"
    if code == 422:
        return "other"
    if 400 <= code < 600:
        return "endpoint_or_model_error"
    return "other"

def classify_llm_error(exc: Exception | None = None, status_code: int | None = None, error_text: str | None = None) -> str:
    # 1. status_code explicit
    if status_code is not None:
        try:
            return _map_status_code(int(status_code))
        except Exception:
            pass
    # 2. exc chain
    if exc is not None:
        cur = exc
        for _ in range(4):  # depth ≤3 includes original; iterate 4 times to cover 0..3
            if cur is None:
                break
            # Timeout / Connection detection
            try:
                import requests as _req  # type: ignore
                if isinstance(cur, _req.exceptions.Timeout):
                    return "timeout"
                if isinstance(cur, _req.exceptions.ConnectionError):
                    return "endpoint_or_model_error"
            except Exception:
                pass
            cname = type(cur).__name__
            # openai SDK detection via class name
            if cname == "APITimeoutError":
                return "timeout"
            if cname == "APIConnectionError":
                return "endpoint_or_model_error"
            # generic substring fallback for Timeout
            if "APITimeout" in cname or (cname == "Timeout" and "requests" not in str(type(cur))):
                # avoid misclassifying requests Timeout already handled
                # but keep simple: if name contains Timeout -> timeout
                if "Timeout" in cname:
                    return "timeout"
            # check response.status_code or status_code
            sc = None
            try:
                resp = getattr(cur, "response", None)
                if resp is not None:
                    sc_attr = getattr(resp, "status_code", None)
                    if sc_attr is not None:
                        return _map_status_code(int(sc_attr))
            except Exception:
                pass
            try:
                sc2 = getattr(cur, "status_code", None)
                if sc2 is not None:
                    return _map_status_code(int(sc2))
            except Exception:
                pass
            # fallback: check cause/context chain
            nxt = getattr(cur, "__cause__", None)
            if nxt is None:
                nxt = getattr(cur, "__context__", None)
            if nxt is None or nxt is cur:
                break
            cur = nxt
        # after chain walk, check Timeout/Connection via name substring for original exc
        # (in case chain depth exceeded or not found status)
        try:
            # broad check: if exc is Timeout subclass but not caught earlier due to import failure
            ename = type(exc).__name__
            if "Timeout" in ename:
                return "timeout"
            if "Connection" in ename and "APIConnection" in ename:
                return "endpoint_or_model_error"
        except Exception:
            pass
        # additional generic detection: requests Timeout/Connection via string
        # if the exc's class module contains requests
        try:
            import requests as _req2  # type: ignore
            if isinstance(exc, _req2.exceptions.Timeout):
                return "timeout"
            if isinstance(exc, _req2.exceptions.ConnectionError):
                return "endpoint_or_model_error"
        except Exception:
            pass
        # also handle openai generic via lower name containing timeout/connection after chain
        # but we already handled; ensure timeout via message? no, that's error_text path
    # 3. error_text
    if error_text is not None:
        txt = str(error_text)
        m = re.search(r"HTTP\s+(\d{3})", txt)
        if m:
            try:
                return _map_status_code(int(m.group(1)))
            except Exception:
                pass
        low = txt.lower()
        # timeout words
        if "timed out" in low or "timeout" in low:
            return "timeout"
        # endpoint patterns: Connection|Failed to resolve|proxy|resolve
        if "connection" in low or "failed to resolve" in low or "proxy" in low or "resolve" in low:
            return "endpoint_or_model_error"
        if "model not found" in low or "not supported" in low or "unsupported" in low:
            return "endpoint_or_model_error"
        if "unauthorized" in low or "authentication" in low or "auth" in low:
            return "auth_failed"
        if "rate" in low or "429" in low:
            return "rate_limited"
    return "other"


def credential_resolver(llm_cfg: dict) -> str:
    """只读复刻 LLMClient 同优先级链"""
    if not isinstance(llm_cfg, dict):
        return ""
    # spec: llm_cfg.api_key or MINIMAX_CN_API_KEY or MINIMAX_API_KEY
    # we also support apiKey camel variant for robustness
    v = llm_cfg.get("api_key", "") or llm_cfg.get("apiKey", "") or ""
    if v:
        return str(v)
    v = os.environ.get("MINIMAX_CN_API_KEY", "") or ""
    if v:
        return str(v)
    v = os.environ.get("MINIMAX_API_KEY", "") or ""
    return str(v) if v else ""


def _extract_llm_cfg(cfg: Any) -> dict:
    if cfg is None:
        return {}
    # V3Config dataclass with .llm
    try:
        if hasattr(cfg, "llm"):
            llm = getattr(cfg, "llm")
            if llm is not None:
                if hasattr(llm, "provider"):
                    return {
                        "provider": str(getattr(llm, "provider", "") or ""),
                        "model": str(getattr(llm, "model", "") or ""),
                        "api_key": str(getattr(llm, "api_key", "") or getattr(llm, "apiKey", "") or ""),
                        "apiKey": str(getattr(llm, "api_key", "") or getattr(llm, "apiKey", "") or ""),
                        "base_url": str(getattr(llm, "base_url", "") or ""),
                    }
                if isinstance(llm, dict):
                    return llm
    except Exception:
        pass
    if isinstance(cfg, dict):
        v = cfg.get("llm")
        if isinstance(v, dict):
            return v
        return {}
    return {}


def build_llm_check(cfg: Any) -> dict[str, Any]:
    llm_cfg = _extract_llm_cfg(cfg)
    provider = str(llm_cfg.get("provider", "") or "")
    model = str(llm_cfg.get("model", "") or "")
    configured = bool(provider and model)
    credential_present = bool(credential_resolver(llm_cfg))
    # raw epochs for logic
    with _state._lock:
        raw_success = _state._last_success_at
        raw_error = _state._last_error_at
        raw_error_type = _state._last_error_type
        obs_success = _state._observer_last_success_at
        obs_error = _state._observer_last_error_at
        obs_error_type = _state._observer_last_error_type
        obs_skip_at = _state._observer_last_skip_at
        obs_skip_reason = _state._observer_last_skip_reason
    snap = snapshot()
    fmt_success = snap.get("last_success_at")
    fmt_error = snap.get("last_error_at")
    fmt_skip = snap.get("observer_last_skip_at")
    # observer 通道
    obs_fail_at = None
    obs_fail_type = None
    if obs_skip_at is not None and (obs_fail_at is None or obs_skip_at > obs_fail_at):
        obs_fail_at = obs_skip_at
        obs_fail_type = obs_skip_reason or "skip"
    if obs_error is not None and (obs_fail_at is None or obs_error > obs_fail_at):
        obs_fail_at = obs_error
        obs_fail_type = obs_error_type or "other"
    obs_failed_now = obs_fail_at is not None and (obs_success is None or obs_fail_at >= obs_success)
    # observer_state
    if obs_failed_now:
        if obs_skip_at is not None and (obs_error is None or obs_skip_at >= obs_error):
            observer_state = "skipped"
        else:
            observer_state = "error"
    else:
        observer_state = "running"
    # 普通通道
    normal_failed_now = raw_error is not None and (raw_success is None or raw_error >= raw_success)
    # overall degraded
    degraded = (not configured) or (not credential_present) or normal_failed_now or obs_failed_now
    if not configured:
        status = "not_configured"
    elif degraded:
        status = "degraded"
    else:
        status = "configured"
    if not configured:
        reason = "not_configured"
    elif not credential_present:
        reason = "credential_missing"
    elif degraded:
        normal_fail_at = raw_error if normal_failed_now else None
        if obs_fail_at is not None and (normal_fail_at is None or obs_fail_at >= normal_fail_at):
            reason = obs_fail_type
        else:
            reason = raw_error_type
    else:
        reason = None
    # text - never include key
    parts: list[str] = []
    parts.append(f"LLM: {status}")
    parts.append(f"credential: {'present' if credential_present else 'missing'}")
    if fmt_success:
        parts.append(f"last success: {fmt_success}")
    else:
        parts.append("last success: none")
    if raw_error_type:
        parts.append(f"last error: {raw_error_type}")
    else:
        parts.append("last error: none")
    parts.append(f"observer: {observer_state}")
    if reason:
        parts.append(f"reason: {reason}")
    text = " | ".join(parts)
    return {
        "configured": configured,
        "credential_present": credential_present,
        "degraded": degraded,
        "status": status,
        "reason": reason,
        "observer_state": observer_state,
        "text": text,
        "last_llm_success_at": fmt_success,
        "last_llm_error_at": fmt_error,
        "last_llm_error_type": raw_error_type,
        "observer_last_skip_at": fmt_skip,
        "observer_last_skip_reason": obs_skip_reason,
        # aliases for broader compatibility
        "last_success_at": fmt_success,
        "last_error_at": fmt_error,
        "last_error_type": raw_error_type,
        "observer_last_success_at": _fmt_time(obs_success),
        "observer_last_error_at": _fmt_time(obs_error),
        "observer_last_error_type": obs_error_type,
    }
