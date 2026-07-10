"""Bounded, payload-safe OpenTelemetry tracing for the indexing worker."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Mapping, Optional
from urllib.parse import unquote, urlparse, urlunparse

from opentelemetry import trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


TRACE_KEYS = frozenset({"traceparent", "tracestate"})
METADATA_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}$")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class TelemetryConfigurationError(RuntimeError):
    """Raised when tracing configuration is unsafe or incomplete."""


class _MappingGetter(Getter[Mapping[str, str]]):
    def get(self, carrier: Mapping[str, str], key: str):
        value = carrier.get(key)
        return [value] if value else None

    def keys(self, carrier: Mapping[str, str]):
        return list(carrier.keys())


_MAPPING_GETTER = _MappingGetter()


def _strict_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise TelemetryConfigurationError(f"{name} must be a boolean")


def _metadata(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not METADATA_PATTERN.fullmatch(normalized):
        raise TelemetryConfigurationError(
            f"{name} must contain 1-128 safe, low-cardinality characters"
        )
    return normalized


def _trace_endpoint(base_endpoint: str, traces_endpoint: str) -> str:
    explicit = traces_endpoint.strip()
    endpoint = explicit or base_endpoint.strip()
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise TelemetryConfigurationError(
            "OTLP trace endpoint must be an http(s) URL without credentials or fragment"
        )
    if explicit:
        return endpoint
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/traces"):
        path = f"{path}/v1/traces" if path else "/v1/traces"
    return urlunparse(parsed._replace(path=path))


def _sampler(name: str, argument: float):
    normalized = name.strip().lower()
    if normalized == "always_on":
        return ALWAYS_ON
    if normalized == "always_off":
        return ALWAYS_OFF
    if normalized == "traceidratio":
        return TraceIdRatioBased(argument)
    if normalized == "parentbased_always_on":
        return ParentBased(ALWAYS_ON)
    if normalized == "parentbased_traceidratio":
        return ParentBased(TraceIdRatioBased(argument))
    raise TelemetryConfigurationError(
        "OTEL_TRACES_SAMPLER must be always_on, always_off, traceidratio, "
        "parentbased_always_on, or parentbased_traceidratio"
    )


def _exporter_headers() -> Dict[str, str]:
    """Parse standard OTLP headers without ever logging their secret values."""
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if not raw:
        return {}
    result: Dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            raise TelemetryConfigurationError(
                "OTEL_EXPORTER_OTLP_HEADERS entries must use key=value"
            )
        encoded_key, encoded_value = item.split("=", 1)
        key = unquote(encoded_key).strip()
        value = unquote(encoded_value).strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key)
            or not value
            or len(value) > 4096
            or "\r" in value
            or "\n" in value
        ):
            raise TelemetryConfigurationError(
                "OTEL_EXPORTER_OTLP_HEADERS contains an invalid entry"
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class TelemetryConfig:
    deployment_profile: str
    sdk_disabled: bool
    traces_exporter: str
    service_name: str
    service_version: str
    deployment_environment: str
    build_id: str
    revision: str
    endpoint: str = ""
    traces_endpoint: str = ""
    exporter_timeout_millis: int = 5000
    schedule_delay_millis: int = 5000
    export_timeout_millis: int = 5000
    max_queue_size: int = 2048
    max_export_batch_size: int = 512
    sampler: str = "parentbased_always_on"
    sampler_argument: float = 1.0

    @classmethod
    def from_env(
        cls,
        *,
        default_service_name: str,
        default_service_version: str,
    ) -> "TelemetryConfig":
        def integer(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, str(default)).strip())
            except ValueError as exc:
                raise TelemetryConfigurationError(f"{name} must be an integer") from exc

        try:
            sampler_argument = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1"))
        except ValueError as exc:
            raise TelemetryConfigurationError(
                "OTEL_TRACES_SAMPLER_ARG must be a number"
            ) from exc
        profile = os.environ.get("DEPLOYMENT_PROFILE", "development").strip().lower()
        return cls(
            deployment_profile=profile,
            sdk_disabled=_strict_bool(
                os.environ.get("OTEL_SDK_DISABLED", "true"),
                "OTEL_SDK_DISABLED",
            ),
            traces_exporter=os.environ.get("OTEL_TRACES_EXPORTER", "none"),
            service_name=os.environ.get("OTEL_SERVICE_NAME", default_service_name),
            service_version=os.environ.get(
                "OTEL_SERVICE_VERSION", default_service_version
            ),
            deployment_environment=os.environ.get(
                "OTEL_DEPLOYMENT_ENVIRONMENT", profile
            ),
            build_id=os.environ.get("OTEL_BUILD_ID", "development"),
            revision=os.environ.get("OTEL_SERVICE_REVISION", "development"),
            endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            traces_endpoint=os.environ.get(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""
            ),
            exporter_timeout_millis=integer("OTEL_EXPORTER_OTLP_TIMEOUT", 5000),
            schedule_delay_millis=integer("OTEL_BSP_SCHEDULE_DELAY", 5000),
            export_timeout_millis=integer("OTEL_BSP_EXPORT_TIMEOUT", 5000),
            max_queue_size=integer("OTEL_BSP_MAX_QUEUE_SIZE", 2048),
            max_export_batch_size=integer("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", 512),
            sampler=os.environ.get(
                "OTEL_TRACES_SAMPLER", "parentbased_always_on"
            ),
            sampler_argument=sampler_argument,
        )

    @property
    def enabled(self) -> bool:
        return not self.sdk_disabled and self.traces_exporter.strip().lower() == "otlp"

    @property
    def resolved_endpoint(self) -> str:
        return _trace_endpoint(self.endpoint, self.traces_endpoint)

    def validate(self) -> None:
        profile = self.deployment_profile.strip().lower()
        exporter = self.traces_exporter.strip().lower()
        if exporter not in {"none", "otlp"}:
            raise TelemetryConfigurationError(
                "OTEL_TRACES_EXPORTER must be 'none' or 'otlp'"
            )
        for value, name in (
            (self.service_name, "OTEL_SERVICE_NAME"),
            (self.service_version, "OTEL_SERVICE_VERSION"),
            (self.deployment_environment, "OTEL_DEPLOYMENT_ENVIRONMENT"),
            (self.build_id, "OTEL_BUILD_ID"),
            (self.revision, "OTEL_SERVICE_REVISION"),
        ):
            _metadata(value, name)
        for value, name in (
            (self.exporter_timeout_millis, "OTEL_EXPORTER_OTLP_TIMEOUT"),
            (self.schedule_delay_millis, "OTEL_BSP_SCHEDULE_DELAY"),
            (self.export_timeout_millis, "OTEL_BSP_EXPORT_TIMEOUT"),
        ):
            if not 100 <= value <= 60000:
                raise TelemetryConfigurationError(
                    f"{name} must be between 100 and 60000 milliseconds"
                )
        if not 1 <= self.max_queue_size <= 65536:
            raise TelemetryConfigurationError(
                "OTEL_BSP_MAX_QUEUE_SIZE must be between 1 and 65536"
            )
        if not 1 <= self.max_export_batch_size <= self.max_queue_size:
            raise TelemetryConfigurationError(
                "OTEL_BSP_MAX_EXPORT_BATCH_SIZE must be positive and no larger than the queue"
            )
        if not 0.0 <= self.sampler_argument <= 1.0:
            raise TelemetryConfigurationError(
                "OTEL_TRACES_SAMPLER_ARG must be between 0 and 1"
            )
        _sampler(self.sampler, self.sampler_argument)
        endpoint = self.resolved_endpoint
        if self.enabled and not endpoint:
            raise TelemetryConfigurationError(
                "OTLP tracing requires OTEL_EXPORTER_OTLP_ENDPOINT or "
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
            )
        _exporter_headers()
        if profile == "enterprise":
            failures = []
            if self.sdk_disabled:
                failures.append("OTEL_SDK_DISABLED=false")
            if exporter != "otlp":
                failures.append("OTEL_TRACES_EXPORTER=otlp")
            if not endpoint or urlparse(endpoint).scheme.lower() != "https":
                failures.append("an HTTPS OTLP trace endpoint")
            if self.sampler.strip().lower() == "always_off" or self.sampler_argument <= 0:
                failures.append("a non-zero trace sampling policy")
            if self.build_id.strip().lower() in {"", "unknown", "development"}:
                failures.append("a release OTEL_BUILD_ID")
            if self.revision.strip().lower() in {"", "unknown", "development"}:
                failures.append("a release OTEL_SERVICE_REVISION")
            if self.deployment_environment.strip().lower() in {
                "",
                "unknown",
                "development",
            }:
                failures.append("a release OTEL_DEPLOYMENT_ENVIRONMENT")
            if failures:
                raise TelemetryConfigurationError(
                    "Enterprise tracing requires: " + ", ".join(failures)
                )


def _safe_carrier(carrier: Optional[Mapping[str, str]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in (carrier or {}).items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(value or "").strip()
        if normalized_key in TRACE_KEYS and normalized_value:
            result[normalized_key] = normalized_value
    return result


class TelemetryRuntime:
    def __init__(
        self,
        config: TelemetryConfig,
        *,
        span_exporter: Optional[SpanExporter] = None,
    ) -> None:
        config.validate()
        self.config = config
        self.enabled = config.enabled
        self._provider: Optional[TracerProvider] = None
        self._tracer = trace.NoOpTracerProvider().get_tracer("imposbro.telemetry")
        self._propagator = TraceContextTextMapPropagator()
        self._shutdown = False
        if not self.enabled:
            return
        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            try:
                span_exporter = OTLPSpanExporter(
                    endpoint=config.resolved_endpoint,
                    headers=_exporter_headers(),
                    timeout=config.exporter_timeout_millis / 1000,
                )
            except Exception as exc:
                raise TelemetryConfigurationError(
                    "Failed to initialize the OTLP/HTTP trace exporter"
                ) from exc
        provider = TracerProvider(
            resource=Resource(
                attributes={
                    "service.name": config.service_name,
                    "service.version": config.service_version,
                    "deployment.environment.name": config.deployment_environment,
                    "imposbro.build.id": config.build_id,
                    "imposbro.service.revision": config.revision,
                }
            ),
            sampler=_sampler(config.sampler, config.sampler_argument),
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=config.max_queue_size,
                schedule_delay_millis=config.schedule_delay_millis,
                max_export_batch_size=config.max_export_batch_size,
                export_timeout_millis=config.export_timeout_millis,
            )
        )
        self._provider = provider
        self._tracer = provider.get_tracer(
            "imposbro.telemetry",
            config.service_version,
        )

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind,
        parent_carrier: Optional[Mapping[str, str]] = None,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> Iterator[Dict[str, str]]:
        if not self.enabled:
            yield _safe_carrier(parent_carrier)
            return
        parent_context = (
            self._propagator.extract(
                carrier=_safe_carrier(parent_carrier),
                getter=_MAPPING_GETTER,
            )
            if parent_carrier
            else None
        )
        with self._tracer.start_as_current_span(
            name,
            context=parent_context,
            kind=kind,
            attributes=dict(attributes or {}),
            record_exception=False,
            set_status_on_exception=False,
        ) as current_span:
            carrier = _safe_carrier(parent_carrier)
            self._propagator.inject(carrier)
            try:
                yield _safe_carrier(carrier)
            except BaseException as exc:
                current_span.set_status(Status(StatusCode.ERROR))
                current_span.set_attribute("error.type", type(exc).__name__)
                raise

    def force_flush(self) -> bool:
        if self._provider is None or self._shutdown:
            return True
        return self._provider.force_flush(self.config.export_timeout_millis)

    def shutdown(self) -> None:
        if self._provider is None or self._shutdown:
            return
        self._shutdown = True
        self._provider.force_flush(self.config.export_timeout_millis)
        self._provider.shutdown()


_runtime = TelemetryRuntime(
    TelemetryConfig(
        deployment_profile="development",
        sdk_disabled=True,
        traces_exporter="none",
        service_name="imposbro-disabled",
        service_version="0",
        deployment_environment="development",
        build_id="development",
        revision="development",
    )
)


def configure_tracing(
    config: TelemetryConfig,
    *,
    span_exporter: Optional[SpanExporter] = None,
) -> TelemetryRuntime:
    global _runtime
    _runtime.shutdown()
    _runtime = TelemetryRuntime(config, span_exporter=span_exporter)
    return _runtime


def get_runtime() -> TelemetryRuntime:
    return _runtime


@contextmanager
def consumer_span(
    parent_carrier: Optional[Mapping[str, str]],
) -> Iterator[Dict[str, str]]:
    with _runtime.span(
        "kafka.process",
        kind=SpanKind.CONSUMER,
        parent_carrier=parent_carrier,
        attributes={
            "messaging.system": "kafka",
            "messaging.operation.type": "process",
        },
    ) as carrier:
        yield carrier


@contextmanager
def typesense_span(operation: str, target_cluster: str) -> Iterator[None]:
    safe_operation = operation if operation in {"upsert", "delete"} else "write"
    candidate_target = str(target_cluster or "").strip()
    safe_target = (
        candidate_target
        if METADATA_PATTERN.fullmatch(candidate_target)
        else "invalid-configured-target"
    )
    with _runtime.span(
        f"typesense.document.{safe_operation}",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system.name": "typesense",
            "db.operation.name": safe_operation,
            "imposbro.target.cluster": safe_target,
        },
    ):
        yield
