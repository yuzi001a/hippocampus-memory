"""Process-level runtime identity, generation, and lifecycle primitives.

Stage 2 deliberately keeps this module independent from the V3Core facade and
from the online PG owners.  Callers provide an already-resolved effective
configuration and an explicit pool factory; later stages may supply the real
PgPool owner without changing the registry invariants here.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .pg_pool import PoolShutdownTimeout
from .topic_recall_cache import TopicRecallCache, TopicRecallCacheCloseTimeout

__all__ = [
    "RuntimeIdentity",
    "RuntimeState",
    "V3Runtime",
    "RuntimeRegistry",
    "canonicalize_profile",
    "canonicalize_hermes_home",
    "compute_config_fingerprint",
    "ConfigFingerprintError",
    "RuntimeCreationError",
    "StaleRuntimeError",
    "RuntimeDrainingError",
    "RuntimeShutdownError",
    "TopicRecallCacheCloseTimeout",
]

_FINGERPRINT_VISIBLE_LENGTH = 16


class ConfigFingerprintError(RuntimeError):
    """The effective configuration cannot be canonicalized reliably."""


class RuntimeCreationError(RuntimeError):
    """A Runtime or its pool could not be constructed."""


class RuntimeShutdownError(RuntimeError):
    """A Runtime did not complete its drain/close operation."""

    def __init__(self, message: str = "runtime shutdown failed", *, failed_count: int = 1):
        self.failed_count = int(failed_count)
        super().__init__(f"{message} ({self.failed_count} runtime(s) not CLOSED)")


class RuntimeState(str, Enum):
    """The only externally visible Runtime lifecycle states."""

    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"


def canonicalize_profile(profile: Any = "default") -> str:
    """Return the canonical logical profile name.

    Empty and whitespace-only values follow the existing default-profile
    behavior.  Profile names are logical identifiers, so unlike Windows file
    paths their case is preserved.
    """
    if profile is None:
        return "default"
    try:
        value = str(profile).strip()
    except Exception:
        raise ValueError("invalid runtime profile") from None
    return value or "default"


def canonicalize_hermes_home(hermes_home: Any = None) -> str | None:
    """Canonicalize a Hermes home without requiring it to exist.

    ``None`` and an empty value mean "home not explicitly specified" and stay
    distinct from every concrete home path.  Existing symlinks are resolved;
    nonexistent paths retain their normalized absolute-path meaning.
    """
    if hermes_home is None:
        return None
    try:
        raw = os.fspath(hermes_home)
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if not isinstance(raw, str):
            raise TypeError
        raw = raw.strip()
    except Exception:
        raise ValueError("invalid hermes_home") from None
    if not raw:
        return None

    try:
        # strict=False is intentional: a new user's home may not exist yet.
        resolved = Path(raw).expanduser().resolve(strict=False)
        value = os.fspath(resolved)
    except (OSError, RuntimeError, TypeError, ValueError):
        try:
            value = os.path.abspath(os.path.normpath(os.path.expanduser(raw)))
        except Exception:
            raise ValueError("invalid hermes_home") from None
    return os.path.normcase(os.path.normpath(value))


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Deterministic process-level identity for one Hermes configuration home."""

    canonical_profile: str
    canonical_hermes_home: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "canonical_profile", canonicalize_profile(self.canonical_profile)
        )
        object.__setattr__(
            self,
            "canonical_hermes_home",
            canonicalize_hermes_home(self.canonical_hermes_home),
        )

    @classmethod
    def from_values(
        cls, profile: Any = "default", hermes_home: Any = None
    ) -> "RuntimeIdentity":
        return cls(
            canonicalize_profile(profile),
            canonicalize_hermes_home(hermes_home),
        )

    @property
    def key(self) -> tuple[str, str | None]:
        """The only registry key representation exposed by this object."""
        return (self.canonical_profile, self.canonical_hermes_home)


# ────────────────────────────────────────────────────────────────
# Effective configuration fingerprinting
# ────────────────────────────────────────────────────────────────
def _canonical_config_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _canonical_config_value(value.value)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonical_config_value(dataclasses.asdict(value))

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigFingerprintError("effective config has a non-string key")
            result[key] = _canonical_config_value(item)
        return result

    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]

    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigFingerprintError("effective config has a non-finite number")
        return value

    if isinstance(value, os.PathLike):
        try:
            return os.fspath(value)
        except Exception:
            raise ConfigFingerprintError("effective config contains an invalid path") from None

    # Sets, bytes, arbitrary objects, and cyclic/opaque values are rejected;
    # silently stringifying them would make generations unstable or ambiguous.
    raise ConfigFingerprintError("effective config contains an unsupported value")


def _compute_config_digest(effective_config: Any) -> str:
    """Return the full internal SHA-256 digest of an effective config."""
    try:
        canonical = _canonical_config_value(effective_config)
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    except ConfigFingerprintError:
        raise
    except Exception:
        # Do not chain the serializer exception: custom objects may contain
        # secrets in their exception text.
        raise ConfigFingerprintError("could not compute config fingerprint") from None
    return digest


def compute_config_fingerprint(effective_config: Any) -> str:
    """Hash an effective config and expose only a short public digest.

    The registry compares the full internal SHA-256 digest; callers and
    observability surfaces receive only the short summary.  Raw configuration
    values are never retained by the Runtime or included in error text.
    """
    return _compute_config_digest(effective_config)[:_FINGERPRINT_VISIBLE_LENGTH]


class StaleRuntimeError(RuntimeError):
    """An ACTIVE identity was requested with a different config generation."""

    def __init__(
        self,
        identity: RuntimeIdentity,
        existing_fingerprint: str,
        requested_fingerprint: str,
    ) -> None:
        self.identity = identity
        self.existing_fingerprint = str(existing_fingerprint)[:_FINGERPRINT_VISIBLE_LENGTH]
        self.requested_fingerprint = str(requested_fingerprint)[:_FINGERPRINT_VISIBLE_LENGTH]
        super().__init__(
            "stale runtime generation for "
            f"profile={identity.canonical_profile!r}, "
            f"home={identity.canonical_hermes_home!r}; "
            f"existing={self.existing_fingerprint}, requested={self.requested_fingerprint}"
        )


class RuntimeDrainingError(RuntimeError):
    """An identity is draining and cannot accept another lease/generation."""

    def __init__(self, identity: RuntimeIdentity) -> None:
        self.identity = identity
        super().__init__(
            "runtime is DRAINING; acquire rejected for "
            f"profile={identity.canonical_profile!r}, "
            f"home={identity.canonical_hermes_home!r}"
        )


class V3Runtime:
    """Shared Runtime container and owner of process-level services."""

    def __init__(
        self,
        identity: RuntimeIdentity,
        config_fingerprint: str,
        pg_pool: Any,
        effective_config: Any = None,
    ) -> None:
        if not isinstance(identity, RuntimeIdentity):
            raise TypeError("identity must be RuntimeIdentity")
        if not isinstance(config_fingerprint, str) or not config_fingerprint:
            raise ValueError("config_fingerprint must be non-empty")
        if pg_pool is None:
            raise ValueError("pg_pool must be provided")
        self.identity = identity
        self._config_fingerprint_full = config_fingerprint
        self._config_fingerprint = config_fingerprint[:_FINGERPRINT_VISIBLE_LENGTH]
        # RuntimeRegistry passes one deep-copied effective-config snapshot here.
        # Facades must consume this object rather than resolving config again.
        self.effective_config = effective_config
        self.pg_pool = pg_pool
        self.topic_recall_cache = TopicRecallCache(
            pool=pg_pool,
            config=effective_config,
        )
        from .config import _resolve_data_dir
        from .runtime_services import E1Service, ObserverService

        state_dir = _resolve_data_dir(effective_config)
        self.e1_service = E1Service(
            config=effective_config,
            pool=pg_pool,
            state_dir=state_dir,
            on_topics_commit=self._notify_topic_recall_invalidation,
        )
        self.observer_service = ObserverService(
            config=effective_config,
            pool=pg_pool,
            e1_service=self.e1_service,
        )
        self._state = RuntimeState.ACTIVE
        self._state_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._services_started = False

    @property
    def config_fingerprint(self) -> str:
        return self._config_fingerprint

    @property
    def state(self) -> RuntimeState:
        with self._state_lock:
            return self._state

    @property
    def lifecycle_state(self) -> RuntimeState:
        return self.state

    def start_services(self) -> None:
        """Start Runtime-owned Observer/E1 services exactly once per Runtime."""
        with self._state_lock:
            if self._state is not RuntimeState.ACTIVE:
                raise RuntimeDrainingError(self.identity)
            if self._services_started:
                return
            self._services_started = True
        started: list[Any] = []
        try:
            for service in (
                getattr(self, "observer_service", None),
                getattr(self, "e1_service", None),
            ):
                start = getattr(service, "start", None)
                if callable(start):
                    start()
                    started.append(service)
        except Exception:
            with self._state_lock:
                self._services_started = False
            for service in reversed(started):
                try:
                    service.close(timeout=0)
                except Exception:
                    pass
            raise

    def _notify_topic_recall_invalidation(self) -> None:
        """Keep Runtime-owned E1 writes on the Stage 5 invalidation seam."""
        cache = getattr(self, "topic_recall_cache", None)
        invalidate = getattr(cache, "invalidate", None)
        if callable(invalidate):
            invalidate()

    @property
    def pool(self) -> Any:
        """Compatibility alias; ownership still belongs to this Runtime."""
        return self.pg_pool

    def begin_draining(self) -> None:
        """Transition ACTIVE -> DRAINING atomically; no-op when already terminal.

        Must be called from inside the registry lock so that observers and
        concurrent acquires linearize on the new state.  A CLOSED runtime is
        left alone; a DRAINING runtime is left alone (idempotent).
        """
        with self._state_lock:
            if self._state is RuntimeState.ACTIVE:
                self._state = RuntimeState.DRAINING
        for service in (
            getattr(self, "observer_service", None),
            getattr(self, "e1_service", None),
        ):
            stop_accepting = getattr(service, "begin_draining", None)
            if callable(stop_accepting):
                try:
                    stop_accepting()
                except Exception:
                    pass

    def finish_shutdown(self, timeout: float | None = 5.0) -> None:
        """Drain the pool and transition DRAINING -> CLOSED.

        The caller is responsible for having already called ``begin_draining``
        (typically under the registry lock) so that the active -> draining
        transition is observable before this method blocks on the cache/pool.
        Observer/E1 service close, cache close, and pool shutdown must all
        succeed before CLOSED; any failed drain leaves the Runtime DRAINING
        so retries remain possible.

        ``KeyboardInterrupt`` and ``SystemExit`` propagate untouched and the
        Runtime stays DRAINING; they are not wrapped into
        ``RuntimeShutdownError`` because they signal caller intent rather
        than a recoverable drain failure.
        """
        with self._shutdown_lock:
            deadline = (
                time.monotonic() + float(timeout) if timeout is not None else None
            )
            failures: list[tuple[str, Exception]] = []

            def close_service(name: str, service: Any) -> None:
                close = getattr(service, "close", None)
                if not callable(close):
                    return
                try:
                    close(
                        timeout=(
                            _remaining_timeout(deadline)
                            if deadline is not None
                            else timeout
                        )
                    )
                except Exception as exc:
                    failures.append((name, exc))

            # Stop Runtime-owned work before closing the resources it may use.
            close_service("observer", getattr(self, "observer_service", None))
            close_service("e1", getattr(self, "e1_service", None))
            close_service("topic_recall_cache", getattr(self, "topic_recall_cache", None))

            try:
                self.pg_pool.shutdown(
                    timeout=(
                        _remaining_timeout(deadline)
                        if deadline is not None
                        else timeout
                    )
                )
            except Exception as exc:
                failures.append(("pg_pool", exc))

            if failures:
                with self._state_lock:
                    self._state = RuntimeState.DRAINING
                name, error = failures[0]
                if isinstance(
                    error,
                    (PoolShutdownTimeout, TopicRecallCacheCloseTimeout, TimeoutError),
                ):
                    raise error
                raise RuntimeShutdownError(
                    f"runtime shutdown failed while closing {name}"
                ) from None

            with self._state_lock:
                self._state = RuntimeState.CLOSED

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """Drain the pool and transition ACTIVE/DRAINING to CLOSED.

        Owner contract: registered runtimes must be shut down through
        ``RuntimeRegistry.shutdown()`` (or ``shutdown_all()``), never by
        business code calling ``runtime.shutdown()`` directly.  This method
        is retained solely for standalone (unregistered) Runtime instances.
        The Registry drives the lifecycle via ``begin_draining()`` and
        ``finish_shutdown()``, and must not call this direct method.

        A failed drain leaves the Runtime DRAINING.  The pool's explicit
        ``PoolShutdownTimeout`` is preserved so callers can distinguish an
        active lease timeout from other shutdown failures.
        """
        with self._state_lock:
            if self._state is RuntimeState.CLOSED:
                return
            self._state = RuntimeState.DRAINING
        self.finish_shutdown(timeout=timeout)

    def stats(self) -> dict[str, Any]:
        """Return an intentionally non-sensitive lifecycle summary."""
        with self._state_lock:
            state = self._state.value
        return {
            "profile": self.identity.canonical_profile,
            "hermes_home": self.identity.canonical_hermes_home,
            "state": state,
            "config_fingerprint": self.config_fingerprint,
        }

    def __repr__(self) -> str:
        return (
            "V3Runtime("
            f"profile={self.identity.canonical_profile!r}, "
            f"home={self.identity.canonical_hermes_home!r}, "
            f"state={self.state.value}, "
            f"fingerprint={self.config_fingerprint!r})"
        )


@dataclass
class _Creation:
    fingerprint: str
    event: threading.Event = field(default_factory=threading.Event)
    cancel_requested: bool = False
    cancel_deadline: float | None = None
    failure: str | None = None


def _missing_pool_factory(_: Any) -> Any:
    raise RuntimeCreationError("pg_pool_factory is not configured")


def _dispose_failed_pool(pool: Any) -> None:
    """Best-effort cleanup without ever rendering pool/config values."""
    if pool is None:
        return
    shutdown = getattr(pool, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(timeout=0)
            return
        except TypeError:
            try:
                shutdown()
                return
            except Exception:
                pass
        except Exception:
            pass
    close = getattr(pool, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _dispose_failed_runtime(
    runtime: Any,
    pool: Any,
    timeout: float | None = None,
    *,
    deadline: float | None = None,
) -> None:
    """Cleanup an unpublished Runtime: services, cache, then pool.

    Order is mandatory. Never reverse it: an orphan service/refresher must not
    outlive pool disposal. ``timeout=None`` waits for workers to actually stop.
    A finite timeout (including 0) uses the services' existing non-blocking
    close semantics instead of waiting forever. ``deadline=`` is an absolute
    ``time.monotonic()`` instant and is used directly when provided so a
    canceller's budget is not restamped via ``monotonic()+remaining``.
    Pool-only factory failures (runtime is None) still go through
    ``_dispose_failed_pool`` only.
    """
    if deadline is None and timeout is not None:
        deadline = time.monotonic() + float(timeout)

    def close_timeout() -> float | None:
        if deadline is not None:
            return _remaining_timeout(deadline)
        return timeout

    if runtime is not None:
        for service in (
            getattr(runtime, "observer_service", None),
            getattr(runtime, "e1_service", None),
        ):
            begin_draining = getattr(service, "begin_draining", None)
            if callable(begin_draining):
                try:
                    begin_draining()
                except Exception:
                    pass
            close_service = getattr(service, "close", None)
            if callable(close_service):
                try:
                    close_service(timeout=close_timeout())
                except Exception:
                    pass
        cache = getattr(runtime, "topic_recall_cache", None)
        close = getattr(cache, "close", None)
        if callable(close):
            try:
                close(timeout=close_timeout())
            except Exception:
                pass
    _dispose_failed_pool(pool)


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _request_creation_cancel(creation: _Creation, deadline: float | None) -> None:
    """Mark an in-flight creation cancelled, keeping the tightest deadline."""
    creation.cancel_requested = True
    if deadline is None:
        return
    previous = creation.cancel_deadline
    creation.cancel_deadline = deadline if previous is None else min(previous, deadline)


class RuntimeRegistry:
    """Thread-safe singleflight registry for one active generation per identity."""

    def __init__(
        self,
        *,
        pg_pool_factory: Callable[[Any], Any] | None = None,
        pool_factory: Callable[[Any], Any] | None = None,
        runtime_factory: Callable[..., V3Runtime] = V3Runtime,
    ) -> None:
        if pg_pool_factory is not None and pool_factory is not None:
            raise ValueError("provide only one pool factory")
        self._pg_pool_factory = pg_pool_factory or pool_factory or _missing_pool_factory
        self._runtime_factory = runtime_factory
        self._lock = threading.RLock()
        self._runtimes: dict[RuntimeIdentity, V3Runtime] = {}
        self._creating: dict[RuntimeIdentity, _Creation] = {}
        # A positive count keeps an identity closed while a shutdown caller is
        # still waiting for its generation to become impossible to publish.
        self._shutdown_requests: dict[RuntimeIdentity, int] = {}
        # New acquires fail while shutdown_all is snapshotting/draining.  This
        # prevents a successor generation from entering between its snapshot
        # and its final empty-registry check.
        self._shutdown_all_in_progress = False

    def acquire(
        self,
        identity: RuntimeIdentity,
        effective_config: Any,
    ) -> V3Runtime:
        """Return the singleton ACTIVE Runtime for an effective generation."""
        if not isinstance(identity, RuntimeIdentity):
            raise TypeError("identity must be RuntimeIdentity")
        try:
            effective_config_snapshot = copy.deepcopy(effective_config)
        except Exception:
            raise ConfigFingerprintError("could not snapshot effective config") from None
        fingerprint = _compute_config_digest(effective_config_snapshot)

        while True:
            with self._lock:
                if self._shutdown_all_in_progress or self._shutdown_requests.get(identity, 0):
                    raise RuntimeDrainingError(identity)

                current = self._runtimes.get(identity)
                if current is not None:
                    state = current.state
                    if state is RuntimeState.ACTIVE:
                        current_fingerprint = getattr(
                            current, "_config_fingerprint_full", current.config_fingerprint
                        )
                        if current_fingerprint == fingerprint:
                            return current
                        raise StaleRuntimeError(
                            identity, current.config_fingerprint, fingerprint
                        )
                    if state is RuntimeState.DRAINING:
                        raise RuntimeDrainingError(identity)
                    # A CLOSED entry is fully drained.  Remove only this exact
                    # generation before allowing construction of its successor.
                    self._runtimes.pop(identity, None)

                creation = self._creating.get(identity)
                if creation is None:
                    creation = _Creation(fingerprint=fingerprint)
                    self._creating[identity] = creation
                    creator = True
                else:
                    creator = False

            if not creator:
                creation.event.wait()
                if creation.failure is not None:
                    raise RuntimeCreationError(creation.failure) from None
                continue
            break

        pool = None
        runtime = None
        published = False
        cancelled = False
        try:
            pool = self._pg_pool_factory(effective_config_snapshot)
            if pool is None:
                raise RuntimeCreationError("pg_pool_factory returned no pool")
            runtime = self._runtime_factory(
                identity=identity,
                config_fingerprint=fingerprint,
                pg_pool=pool,
                effective_config=effective_config_snapshot,
            )
            if runtime is None:
                raise RuntimeCreationError("runtime factory returned no runtime")

            with self._lock:
                # The creator may have been canceled while the factory ran.
                # Keep the creation slot until cleanup has completed so a
                # shutdown caller can prove this generation cannot publish.
                if creation.cancel_requested or self._shutdown_requests.get(identity, 0):
                    cancelled = True
                else:
                    self._runtimes[identity] = runtime
                    self._creating.pop(identity, None)
                    published = True
                    creation.event.set()
                    return runtime

            raise RuntimeCreationError("runtime creation cancelled")
        except Exception:
            if cancelled:
                raise RuntimeCreationError("runtime creation cancelled") from None
            raise RuntimeCreationError("runtime construction failed") from None
        finally:
            if not published:
                try:
                    if runtime is not None:
                        _dispose_failed_runtime(
                            runtime, pool, deadline=creation.cancel_deadline
                        )
                    else:
                        _dispose_failed_pool(pool)
                finally:
                    reason = (
                        "runtime creation cancelled"
                        if creation.cancel_requested
                        else "runtime construction failed"
                    )
                    self._complete_creation(identity, creation, reason)

    def _complete_creation(
        self, identity: RuntimeIdentity, creation: _Creation, reason: str
    ) -> None:
        """Remove a failed/canceled creation only after its resources are clean."""
        with self._lock:
            if self._creating.get(identity) is creation:
                creation.failure = reason
                self._creating.pop(identity, None)
                creation.event.set()

    def _add_shutdown_request_locked(self, identity: RuntimeIdentity) -> None:
        self._shutdown_requests[identity] = self._shutdown_requests.get(identity, 0) + 1

    def _release_shutdown_request_locked(self, identity: RuntimeIdentity) -> None:
        count = self._shutdown_requests.get(identity, 0)
        if count <= 1:
            self._shutdown_requests.pop(identity, None)
        else:
            self._shutdown_requests[identity] = count - 1

    def shutdown(self, identity: RuntimeIdentity, timeout: float | None = 5.0) -> None:
        """Drain one generation; retain the entry on every failure."""
        if not isinstance(identity, RuntimeIdentity):
            raise TypeError("identity must be RuntimeIdentity")

        deadline = (
            time.monotonic() + float(timeout) if timeout is not None else None
        )
        runtime: V3Runtime | None = None
        wait_event: threading.Event | None = None
        with self._lock:
            runtime = self._runtimes.get(identity)
            if runtime is not None:
                # This barrier covers the CLOSED-before-registry-pop window
                # as well as the slow pool drain.
                self._add_shutdown_request_locked(identity)
                runtime.begin_draining()
            else:
                creation = self._creating.get(identity)
                if creation is None:
                    return
                self._add_shutdown_request_locked(identity)
                _request_creation_cancel(creation, deadline)
                wait_event = creation.event

        if wait_event is not None:
            try:
                # The creator signals only after the pool has been disposed and
                # the _creating entry has been removed.
                if not wait_event.wait(timeout=_remaining_timeout(deadline)):
                    raise RuntimeShutdownError("runtime shutdown timed out waiting for creation")
            finally:
                with self._lock:
                    self._release_shutdown_request_locked(identity)
            return

        assert runtime is not None
        try:
            runtime.finish_shutdown(timeout=_remaining_timeout(deadline))
        finally:
            with self._lock:
                if (
                    self._runtimes.get(identity) is runtime
                    and runtime.state is RuntimeState.CLOSED
                ):
                    self._runtimes.pop(identity, None)
                self._release_shutdown_request_locked(identity)

    def shutdown_all(self, timeout: float | None = 5.0) -> None:
        """Drain every Runtime and wait out every in-flight creation."""
        deadline = (
            time.monotonic() + float(timeout) if timeout is not None else None
        )
        failures: list[RuntimeIdentity] = []
        creation_identities: list[RuntimeIdentity] = []

        def record_failure(identity: RuntimeIdentity) -> None:
            if identity not in failures:
                failures.append(identity)

        try:
            with self._lock:
                self._shutdown_all_in_progress = True
                items = list(self._runtimes.items())
                creations = list(self._creating.items())
                for identity, creation in creations:
                    creation_identities.append(identity)
                    self._add_shutdown_request_locked(identity)
                    _request_creation_cancel(creation, deadline)
                # Make the state transition visible before any slow finish.
                for _, runtime in items:
                    runtime.begin_draining()

            for identity, creation in creations:
                # _complete_creation signals only after construction cleanup;
                # no creation can publish after this wait completes.
                if not creation.event.wait(timeout=_remaining_timeout(deadline)):
                    record_failure(identity)

            for identity, runtime in items:
                try:
                    runtime.finish_shutdown(timeout=_remaining_timeout(deadline))
                except Exception:
                    record_failure(identity)
                    continue
                with self._lock:
                    if (
                        self._runtimes.get(identity) is runtime
                        and runtime.state is RuntimeState.CLOSED
                    ):
                        self._runtimes.pop(identity, None)
                    elif runtime.state is not RuntimeState.CLOSED:
                        record_failure(identity)

            with self._lock:
                for identity in self._creating:
                    record_failure(identity)
                for identity in self._runtimes:
                    record_failure(identity)

            if failures:
                raise RuntimeShutdownError(
                    "one or more runtimes failed to drain",
                    failed_count=len(failures),
                )
        finally:
            with self._lock:
                for identity in creation_identities:
                    self._release_shutdown_request_locked(identity)
                self._shutdown_all_in_progress = False

    def stats(self) -> dict[str, Any]:
        with self._lock:
            items = sorted(
                self._runtimes.items(),
                key=lambda item: (
                    item[0].canonical_profile,
                    item[0].canonical_hermes_home or "",
                ),
            )
            runtimes = [runtime.stats() for _, runtime in items]
            return {
                "count": len(items),
                "creating": len(self._creating),
                "runtimes": runtimes,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._runtimes)

    def __repr__(self) -> str:
        with self._lock:
            return (
                "RuntimeRegistry("
                f"runtimes={len(self._runtimes)}, "
                f"creating={len(self._creating)})"
            )