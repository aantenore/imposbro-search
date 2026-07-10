{{/* vim: set filetype=gotemplate: */}}
{{/*
Helper functions for the chart.
*/}}

{{/*
Generates the full chart name.
If the chart name is "my-chart" and the release name is "my-release",
it returns "my-release-my-chart".
*/}}
{{- define "imposbro-search.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Creates standard chart selector labels.
*/}}
{{- define "imposbro-search.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Creates standard chart labels.
*/}}
{{- define "imposbro-search.labels" -}}
{{- include "imposbro-search.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Resolves the service account name used by workloads.
*/}}
{{- define "imposbro-search.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "imposbro-search.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Resolve the single Kubernetes Secret consumed by all workloads. */}}
{{- define "imposbro-search.secretName" -}}
{{- if .Values.config.useSecret -}}
{{- printf "%s-secret" (include "imposbro-search.fullname" .) -}}
{{- else if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else if .Values.secrets.externalSecret.enabled -}}
{{- default (printf "%s-secret" (include "imposbro-search.fullname" .)) .Values.secrets.externalSecret.target.name -}}
{{- end -}}
{{- end }}

{{- define "imposbro-search.kedaAuthenticationName" -}}
{{- default (printf "%s-indexing-kafka-auth" (include "imposbro-search.fullname" .)) .Values.indexingService.keda.authenticationRef.name -}}
{{- end }}

{{/* A stable rollout token for externally managed secret rotation. */}}
{{- define "imposbro-search.secretSourceChecksum" -}}
{{- toJson (dict "name" (include "imposbro-search.secretName" .) "rolloutVersion" .Values.secrets.rolloutVersion "externalSecret" .Values.secrets.externalSecret) | sha256sum -}}
{{- end }}

{{/*
Render explicit Secret key references for one process. A declared key is
required at pod admission; an undeclared optional key is loaded when present.
This avoids envFrom leaking every credential in a shared Secret to every
workload while preserving existing Secret and ExternalSecret integrations.
*/}}
{{- define "imposbro-search.secretEnv" -}}
{{- $root := .root -}}
{{- $declared := $root.Values.secrets.providedKeys | default (list) -}}
{{- range $key := .keys }}
- name: {{ $key | quote }}
  valueFrom:
    secretKeyRef:
      name: {{ include "imposbro-search.secretName" $root | quote }}
      key: {{ $key | quote }}
      optional: {{ not (has $key $declared) }}
{{- end -}}
{{- end }}

{{/*
Map persisted Typesense env: references to differently named process variables.
The Kubernetes Secret keeps stable source keys, while the application stores
only the non-secret reference and therefore never persists a raw credential.
*/}}
{{- define "imposbro-search.typesenseSecretRefEnv" -}}
{{- $root := . -}}
{{- $declared := $root.Values.secrets.providedKeys | default (list) -}}
{{- $sources := list
  (dict "ref" $root.Values.config.INTERNAL_STATE_API_KEY_REF "key" "INTERNAL_STATE_API_KEY")
  (dict "ref" $root.Values.config.DEFAULT_DATA_CLUSTER_API_KEY_REF "key" "DEFAULT_DATA_CLUSTER_API_KEY")
  (dict "ref" $root.Values.config.DEFAULT_DATA2_CLUSTER_API_KEY_REF "key" "DEFAULT_DATA2_CLUSTER_API_KEY") -}}
{{- range $source := $sources -}}
{{- $ref := toString $source.ref -}}
{{- if hasPrefix "env:" $ref }}
- name: {{ trimPrefix "env:" $ref | quote }}
  valueFrom:
    secretKeyRef:
      name: {{ include "imposbro-search.secretName" $root | quote }}
      key: {{ $source.key | quote }}
      optional: {{ not (has $source.key $declared) }}
{{- end -}}
{{- end -}}
{{- end }}

{{/* Render component-scoped anti-affinity without hardcoding release names. */}}
{{- define "imposbro-search.podAntiAffinity" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $type := lower (toString $root.Values.availability.podAntiAffinity.type) -}}
podAntiAffinity:
  {{- if eq $type "required" }}
  requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels:
          {{- include "imposbro-search.selectorLabels" $root | nindent 10 }}
          app.kubernetes.io/component: {{ $component }}
      topologyKey: {{ $root.Values.availability.podAntiAffinity.topologyKey | quote }}
  {{- else }}
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            {{- include "imposbro-search.selectorLabels" $root | nindent 12 }}
            app.kubernetes.io/component: {{ $component }}
        topologyKey: {{ $root.Values.availability.podAntiAffinity.topologyKey | quote }}
  {{- end }}
{{- end }}

{{/*
Require production callers to provide immutable, non-placeholder images.
*/}}
{{- define "imposbro-search.requiredImage" -}}
{{- $name := .name -}}
{{- $value := required (printf "%s is required and must be an immutable image reference" $name) .value -}}
{{- if or (contains "your-registry/" $value) (contains "your-registry-user/" $value) -}}
{{- fail (printf "%s must be a non-placeholder image reference (got %q)" $name $value) -}}
{{- end -}}
{{- if or (hasSuffix ":latest" $value) (contains ":latest@" $value) -}}
{{- fail (printf "%s must not use the mutable :latest tag (got %q)" $name $value) -}}
{{- end -}}
{{- if not (regexMatch "@sha256:[A-Fa-f0-9]{64}$" $value) -}}
{{- fail (printf "%s must be pinned by digest with @sha256:<64 hex chars> (got %q)" $name $value) -}}
{{- end -}}
{{- $value -}}
{{- end }}

{{/*
Require production OIDC endpoints to use HTTPS unless explicitly disabled for local-only testing.
*/}}
{{- define "imposbro-search.validateSecureOidcUrl" -}}
{{- $name := .name -}}
{{- $value := toString .value -}}
{{- $allowInsecure := eq (toString .allowInsecure) "true" -}}
{{- if and $value (not $allowInsecure) (not (hasPrefix "https://" $value)) -}}
{{- fail (printf "%s must use https:// unless config.ALLOW_INSECURE_OIDC_URLS=true for local-only testing (got %q)" $name $value) -}}
{{- end -}}
{{- end }}

{{/* Validate Admin UI OIDC URLs, permitting HTTP only for explicit loopback development. */}}
{{- define "imposbro-search.validateAdminUiOidcUrl" -}}
{{- $name := .name -}}
{{- $value := toString .value -}}
{{- $allowInsecureLocalhost := eq (toString .allowInsecureLocalhost) "true" -}}
{{- if $value -}}
{{- if or (contains "@" $value) (contains "#" $value) -}}
{{- fail (printf "%s must not contain credentials or fragments" $name) -}}
{{- end -}}
{{- $secure := hasPrefix "https://" $value -}}
{{- $loopback := regexMatch "^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?(/.*)?$" $value -}}
{{- if not (or $secure (and $allowInsecureLocalhost $loopback)) -}}
{{- fail (printf "%s must use https:// (HTTP is allowed only for explicitly enabled loopback development)" $name) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Render scoped API keys as the JSON string expected by the application.
Allows either the documented string value or Helm --set-json list input.
*/}}
{{- define "imposbro-search.scopedApiKeysJson" -}}
{{- if kindIs "string" .Values.config.SCOPED_API_KEYS -}}
{{- .Values.config.SCOPED_API_KEYS -}}
{{- else -}}
{{- toJson .Values.config.SCOPED_API_KEYS -}}
{{- end -}}
{{- end }}

{{/*
Validate required external service and secret configuration.
*/}}
{{- define "imposbro-search.validateConfig" -}}
{{- $profile := lower (toString .Values.config.DEPLOYMENT_PROFILE) -}}
{{- if and (ne $profile "development") (ne $profile "production") (ne $profile "enterprise") (ne $profile "test") -}}
{{- fail "config.DEPLOYMENT_PROFILE must be development, test, production, or enterprise" -}}
{{- end -}}
{{- $otelSdkDisabled := lower (toString .Values.config.OTEL_SDK_DISABLED) -}}
{{- if not (has $otelSdkDisabled (list "true" "false")) -}}
{{- fail "config.OTEL_SDK_DISABLED must be true or false" -}}
{{- end -}}
{{- $otelExporter := lower (toString .Values.config.OTEL_TRACES_EXPORTER) -}}
{{- if not (has $otelExporter (list "none" "otlp")) -}}
{{- fail "config.OTEL_TRACES_EXPORTER must be none or otlp" -}}
{{- end -}}
{{- $otelEndpoint := toString (default .Values.config.OTEL_EXPORTER_OTLP_ENDPOINT .Values.config.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) -}}
{{- if and (eq $otelSdkDisabled "false") (eq $otelExporter "otlp") (not $otelEndpoint) -}}
{{- fail "enabled OTLP tracing requires config.OTEL_EXPORTER_OTLP_ENDPOINT or config.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" -}}
{{- end -}}
{{- range $name, $value := dict "OTEL_EXPORTER_OTLP_TIMEOUT" .Values.config.OTEL_EXPORTER_OTLP_TIMEOUT "OTEL_BSP_SCHEDULE_DELAY" .Values.config.OTEL_BSP_SCHEDULE_DELAY "OTEL_BSP_EXPORT_TIMEOUT" .Values.config.OTEL_BSP_EXPORT_TIMEOUT -}}
{{- if or (lt (int $value) 100) (gt (int $value) 60000) -}}
{{- fail (printf "config.%s must be between 100 and 60000 milliseconds" $name) -}}
{{- end -}}
{{- end -}}
{{- if or (lt (int .Values.config.OTEL_BSP_MAX_QUEUE_SIZE) 1) (gt (int .Values.config.OTEL_BSP_MAX_QUEUE_SIZE) 65536) -}}
{{- fail "config.OTEL_BSP_MAX_QUEUE_SIZE must be between 1 and 65536" -}}
{{- end -}}
{{- if or (lt (int .Values.config.OTEL_BSP_MAX_EXPORT_BATCH_SIZE) 1) (gt (int .Values.config.OTEL_BSP_MAX_EXPORT_BATCH_SIZE) (int .Values.config.OTEL_BSP_MAX_QUEUE_SIZE)) -}}
{{- fail "config.OTEL_BSP_MAX_EXPORT_BATCH_SIZE must be positive and no larger than OTEL_BSP_MAX_QUEUE_SIZE" -}}
{{- end -}}
{{- $otelSampler := lower (toString .Values.config.OTEL_TRACES_SAMPLER) -}}
{{- if not (has $otelSampler (list "always_on" "always_off" "traceidratio" "parentbased_always_on" "parentbased_traceidratio")) -}}
{{- fail "config.OTEL_TRACES_SAMPLER is unsupported" -}}
{{- end -}}
{{- if or (lt (float64 .Values.config.OTEL_TRACES_SAMPLER_ARG) 0.0) (gt (float64 .Values.config.OTEL_TRACES_SAMPLER_ARG) 1.0) -}}
{{- fail "config.OTEL_TRACES_SAMPLER_ARG must be between 0 and 1" -}}
{{- end -}}
{{- $managedSecret := .Values.config.useSecret -}}
{{- $existingSecret := ne (toString .Values.secrets.existingSecret) "" -}}
{{- $externalSecret := .Values.secrets.externalSecret.enabled -}}
{{- $secretSourceCount := add (ternary 1 0 $managedSecret) (ternary 1 0 $existingSecret) (ternary 1 0 $externalSecret) -}}
{{- if ne (int $secretSourceCount) 1 -}}
{{- fail "exactly one secret source is required: config.useSecret, secrets.existingSecret, or secrets.externalSecret.enabled" -}}
{{- end -}}
{{- $declaredSecretKeys := .Values.secrets.providedKeys | default (list) -}}
{{- range $workload, $keys := .Values.secrets.workloadKeys -}}
{{- $seen := dict -}}
{{- range $key := $keys -}}
{{- if not (regexMatch "^[A-Z][A-Z0-9_]*$" (toString $key)) -}}
{{- fail (printf "secrets.workloadKeys.%s contains invalid environment key %q" $workload $key) -}}
{{- end -}}
{{- if hasKey $seen (toString $key) -}}
{{- fail (printf "secrets.workloadKeys.%s contains duplicate key %s" $workload $key) -}}
{{- end -}}
{{- $_ := set $seen (toString $key) true -}}
{{- end -}}
{{- end -}}
{{- $indexingEventStoreBackend := lower (toString .Values.config.INDEXING_EVENT_STORE_BACKEND) -}}
{{- if not (has $indexingEventStoreBackend (list "disabled" "memory" "postgres")) -}}
{{- fail "config.INDEXING_EVENT_STORE_BACKEND must be disabled, memory, or postgres" -}}
{{- end -}}
{{- if or (le (float64 .Values.config.INDEXING_OUTBOX_POLL_SECONDS) 0.0) (gt (float64 .Values.config.INDEXING_OUTBOX_POLL_SECONDS) 300.0) -}}
{{- fail "config.INDEXING_OUTBOX_POLL_SECONDS must be > 0 and <= 300" -}}
{{- end -}}
{{- if or (lt (int .Values.config.INDEXING_OUTBOX_BATCH_SIZE) 1) (gt (int .Values.config.INDEXING_OUTBOX_BATCH_SIZE) 1000) -}}
{{- fail "config.INDEXING_OUTBOX_BATCH_SIZE must be between 1 and 1000" -}}
{{- end -}}
{{- if and (eq $indexingEventStoreBackend "postgres") (not .Values.config.CONTROL_PLANE_DATABASE_URL) (not (has "CONTROL_PLANE_DATABASE_URL" $declaredSecretKeys)) -}}
{{- fail "config.INDEXING_EVENT_STORE_BACKEND=postgres requires CONTROL_PLANE_DATABASE_URL from the selected Secret" -}}
{{- end -}}
{{- if and (eq $indexingEventStoreBackend "memory") (or .Values.queryApi.autoscaling.enabled (gt (int .Values.queryApi.replicaCount) 1)) -}}
{{- fail "config.INDEXING_EVENT_STORE_BACKEND=memory is only supported for a single Query API replica" -}}
{{- end -}}
{{- $checkpointBackend := lower (toString .Values.config.INDEXING_CHECKPOINT_BACKEND) -}}
{{- if not (has $checkpointBackend (list "postgres" "memory" "typesense")) -}}
{{- fail "config.INDEXING_CHECKPOINT_BACKEND must be postgres, memory, or typesense" -}}
{{- end -}}
{{- $allowVolatileCheckpoints := lower (toString .Values.config.INDEXING_ALLOW_VOLATILE_CHECKPOINTS) -}}
{{- $allowTypesenseCheckpoints := lower (toString .Values.config.INDEXING_ALLOW_TYPESENSE_CHECKPOINTS) -}}
{{- if not (has $allowVolatileCheckpoints (list "true" "false")) -}}
{{- fail "config.INDEXING_ALLOW_VOLATILE_CHECKPOINTS must be true or false" -}}
{{- end -}}
{{- if not (has $allowTypesenseCheckpoints (list "true" "false")) -}}
{{- fail "config.INDEXING_ALLOW_TYPESENSE_CHECKPOINTS must be true or false" -}}
{{- end -}}
{{- if or (lt (int .Values.config.INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS) 1) (gt (int .Values.config.INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS) 60000) -}}
{{- fail "config.INDEXING_CHECKPOINT_LOCK_TIMEOUT_MS must be between 1 and 60000" -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9_-]{1,128}$" (toString .Values.config.INDEXING_CHECKPOINT_COLLECTION)) -}}
{{- fail "config.INDEXING_CHECKPOINT_COLLECTION must be a safe collection name" -}}
{{- end -}}
{{- if and (eq $checkpointBackend "postgres") (not .Values.config.CONTROL_PLANE_DATABASE_URL) (not (has "CONTROL_PLANE_DATABASE_URL" $declaredSecretKeys)) -}}
{{- fail "config.INDEXING_CHECKPOINT_BACKEND=postgres requires CONTROL_PLANE_DATABASE_URL from the selected Secret" -}}
{{- end -}}
{{- if and (eq $profile "enterprise") (ne $checkpointBackend "postgres") -}}
{{- fail "enterprise profile requires config.INDEXING_CHECKPOINT_BACKEND=postgres" -}}
{{- end -}}
{{- if and (eq $profile "production") (ne $checkpointBackend "postgres") -}}
{{- fail "production profile requires config.INDEXING_CHECKPOINT_BACKEND=postgres" -}}
{{- end -}}
{{- if and (eq $checkpointBackend "memory") (ne $allowVolatileCheckpoints "true") -}}
{{- fail "config.INDEXING_CHECKPOINT_BACKEND=memory requires INDEXING_ALLOW_VOLATILE_CHECKPOINTS=true" -}}
{{- end -}}
{{- if and (eq $checkpointBackend "typesense") (ne $allowTypesenseCheckpoints "true") -}}
{{- fail "config.INDEXING_CHECKPOINT_BACKEND=typesense requires INDEXING_ALLOW_TYPESENSE_CHECKPOINTS=true" -}}
{{- end -}}
{{- if $externalSecret -}}
{{- $_ := required "secrets.externalSecret.apiVersion is required when ExternalSecret is enabled" .Values.secrets.externalSecret.apiVersion -}}
{{- $_ := required "secrets.externalSecret.secretStoreRef.name is required when ExternalSecret is enabled" .Values.secrets.externalSecret.secretStoreRef.name -}}
{{- if not (has (toString .Values.secrets.externalSecret.secretStoreRef.kind) (list "SecretStore" "ClusterSecretStore")) -}}
{{- fail "secrets.externalSecret.secretStoreRef.kind must be SecretStore or ClusterSecretStore" -}}
{{- end -}}
{{- if not (has (toString .Values.secrets.externalSecret.target.creationPolicy) (list "Owner" "Orphan" "Merge" "None")) -}}
{{- fail "secrets.externalSecret.target.creationPolicy is invalid" -}}
{{- end -}}
{{- if not (has (toString .Values.secrets.externalSecret.target.deletionPolicy) (list "Retain" "Delete" "Merge")) -}}
{{- fail "secrets.externalSecret.target.deletionPolicy is invalid" -}}
{{- end -}}
{{- if and (not .Values.secrets.externalSecret.data) (not .Values.secrets.externalSecret.dataFrom) -}}
{{- fail "secrets.externalSecret.data or secrets.externalSecret.dataFrom is required when ExternalSecret is enabled" -}}
{{- end -}}
{{- range .Values.secrets.externalSecret.data -}}
{{- $_ := required "secrets.externalSecret.data[].secretKey is required" .secretKey -}}
{{- $_ := required "secrets.externalSecret.data[].remoteRef.key is required" .remoteRef.key -}}
{{- end -}}
{{- end -}}
{{- $_ := required "config.KAFKA_BROKER_URL is required; deploy Kafka separately and provide its bootstrap URL" .Values.config.KAFKA_BROKER_URL -}}
{{- if and (not .Values.config.REDIS_URL) (not (has "REDIS_URL" $declaredSecretKeys)) -}}
{{- fail "config.REDIS_URL or declared external Secret key REDIS_URL is required" -}}
{{- end -}}
{{- $_ := required "config.INTERNAL_STATE_NODES is required; deploy Typesense state nodes separately and provide hostnames" .Values.config.INTERNAL_STATE_NODES -}}
{{- $_ := required "config.DEFAULT_DATA_CLUSTER_NODES is required; deploy Typesense data nodes separately and provide hostnames" .Values.config.DEFAULT_DATA_CLUSTER_NODES -}}
{{- $kafkaSecurityProtocol := upper (toString .Values.config.KAFKA_SECURITY_PROTOCOL) -}}
{{- if not (has $kafkaSecurityProtocol (list "PLAINTEXT" "SSL" "SASL_PLAINTEXT" "SASL_SSL")) -}}
{{- fail "config.KAFKA_SECURITY_PROTOCOL must be PLAINTEXT, SSL, SASL_PLAINTEXT, or SASL_SSL" -}}
{{- end -}}
{{- if contains "SASL" $kafkaSecurityProtocol -}}
{{- $saslMechanism := upper (toString .Values.config.KAFKA_SASL_MECHANISM) -}}
{{- if not (has $saslMechanism (list "PLAIN" "SCRAM-SHA-256" "SCRAM-SHA-512")) -}}
{{- fail "config.KAFKA_SASL_MECHANISM must be PLAIN, SCRAM-SHA-256, or SCRAM-SHA-512 for SASL transports" -}}
{{- end -}}
{{- if and (not .Values.config.KAFKA_SASL_USERNAME) (not (has "KAFKA_SASL_USERNAME" $declaredSecretKeys)) -}}
{{- fail "KAFKA_SASL_USERNAME must be supplied by the selected Secret for SASL transports" -}}
{{- end -}}
{{- if and (not .Values.config.KAFKA_SASL_PASSWORD) (not (has "KAFKA_SASL_PASSWORD" $declaredSecretKeys)) -}}
{{- fail "KAFKA_SASL_PASSWORD must be supplied by the selected Secret for SASL transports" -}}
{{- end -}}
{{- end -}}
{{- if and (contains "SSL" $kafkaSecurityProtocol) (not .Values.tls.trustBundle.enabled) -}}
{{- fail "tls.trustBundle.enabled=true is required for Kafka SSL transports" -}}
{{- end -}}
{{- $internalStateProtocol := toString .Values.config.INTERNAL_STATE_PROTOCOL -}}
{{- if and (ne $internalStateProtocol "http") (ne $internalStateProtocol "https") -}}
{{- fail "config.INTERNAL_STATE_PROTOCOL must be http or https" -}}
{{- end -}}
{{- $defaultDataProtocol := toString .Values.config.DEFAULT_DATA_CLUSTER_PROTOCOL -}}
{{- if and (ne $defaultDataProtocol "http") (ne $defaultDataProtocol "https") -}}
{{- fail "config.DEFAULT_DATA_CLUSTER_PROTOCOL must be http or https" -}}
{{- end -}}
{{- $defaultData2Protocol := toString .Values.config.DEFAULT_DATA2_CLUSTER_PROTOCOL -}}
{{- if and (ne $defaultData2Protocol "http") (ne $defaultData2Protocol "https") -}}
{{- fail "config.DEFAULT_DATA2_CLUSTER_PROTOCOL must be http or https" -}}
{{- end -}}
{{- $_ := required "config.INTERNAL_QUERY_API_URL is required for Admin UI and indexing service discovery" .Values.config.INTERNAL_QUERY_API_URL -}}
{{- $_ := required "config.REQUEST_ID_HEADER is required for HTTP/Kafka request correlation" .Values.config.REQUEST_ID_HEADER -}}
{{- $readinessPolicy := toString .Values.config.READINESS_POLICY -}}
{{- if and (ne $readinessPolicy "serving") (ne $readinessPolicy "strict") -}}
{{- fail "config.READINESS_POLICY must be serving or strict" -}}
{{- end -}}
{{- if le (float64 .Values.config.QUERY_API_HEALTH_CACHE_TTL_SECONDS) 0.0 -}}
{{- fail "config.QUERY_API_HEALTH_CACHE_TTL_SECONDS must be > 0" -}}
{{- end -}}
{{- if le (float64 .Values.config.QUERY_API_HEALTH_CHECK_BUDGET_SECONDS) 0.0 -}}
{{- fail "config.QUERY_API_HEALTH_CHECK_BUDGET_SECONDS must be > 0" -}}
{{- end -}}
{{- if le (float64 .Values.config.QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS) 0.0 -}}
{{- fail "config.QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS must be > 0" -}}
{{- end -}}
{{- if lt (int .Values.config.QUERY_API_HEALTH_MAX_WORKERS) 3 -}}
{{- fail "config.QUERY_API_HEALTH_MAX_WORKERS must be >= 3" -}}
{{- end -}}
{{- if ge (float64 .Values.config.QUERY_API_HEALTH_CHECK_BUDGET_SECONDS) (float64 .Values.queryApi.readinessProbe.timeoutSeconds) -}}
{{- fail "config.QUERY_API_HEALTH_CHECK_BUDGET_SECONDS must be lower than queryApi.readinessProbe.timeoutSeconds" -}}
{{- end -}}
{{- if or (lt (int .Values.indexingService.health.port) 1) (gt (int .Values.indexingService.health.port) 65535) -}}
{{- fail "indexingService.health.port must be between 1 and 65535" -}}
{{- end -}}
{{- if or (hasKey .Values.podAnnotations "checksum/config") (hasKey .Values.podAnnotations "checksum/secret") (hasKey .Values.podAnnotations "checksum/secret-source") -}}
{{- fail "podAnnotations must not override reserved checksum/config, checksum/secret, or checksum/secret-source annotations" -}}
{{- end -}}
{{- if and (not .Values.config.INTERNAL_STATE_API_KEY) (not (has "INTERNAL_STATE_API_KEY" $declaredSecretKeys)) -}}
{{- fail "config.INTERNAL_STATE_API_KEY or declared external Secret key INTERNAL_STATE_API_KEY is required" -}}
{{- end -}}
{{- if and (not .Values.config.DEFAULT_DATA_CLUSTER_API_KEY) (not (has "DEFAULT_DATA_CLUSTER_API_KEY" $declaredSecretKeys)) -}}
{{- fail "config.DEFAULT_DATA_CLUSTER_API_KEY or declared external Secret key DEFAULT_DATA_CLUSTER_API_KEY is required" -}}
{{- end -}}
{{- $oidcEnabled := eq (toString .Values.config.OIDC_ENABLED) "true" -}}
{{- $allowInsecureOidcUrls := eq (toString .Values.config.ALLOW_INSECURE_OIDC_URLS) "true" -}}
{{- $hasAdminKey := or .Values.config.ADMIN_API_KEY (has "ADMIN_API_KEY" $declaredSecretKeys) -}}
{{- $hasDataKey := or .Values.config.DATA_API_KEY (has "DATA_API_KEY" $declaredSecretKeys) -}}
{{- $hasScopedKeys := or .Values.config.SCOPED_API_KEYS (has "SCOPED_API_KEYS" $declaredSecretKeys) -}}
{{- $hasInternalAdminKey := or .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY (has "INTERNAL_QUERY_API_ADMIN_API_KEY" $declaredSecretKeys) -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") (not $hasAdminKey) (not $hasScopedKeys) (not $oidcEnabled) -}}
{{- fail "config.ADMIN_API_KEY, config.SCOPED_API_KEYS, or config.OIDC_ENABLED=true is required when ALLOW_UNAUTHENTICATED_ADMIN is false" -}}
{{- end -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_DATA) "false") (not $hasDataKey) (not $hasScopedKeys) (not $oidcEnabled) -}}
{{- fail "config.DATA_API_KEY, config.SCOPED_API_KEYS, or config.OIDC_ENABLED=true is required when ALLOW_UNAUTHENTICATED_DATA is false" -}}
{{- end -}}
{{- $adminApiKey := toString .Values.config.ADMIN_API_KEY -}}
{{- $internalAdminApiKey := toString .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") (not $hasAdminKey) (not $hasInternalAdminKey) -}}
{{- fail "config.ADMIN_API_KEY or config.INTERNAL_QUERY_API_ADMIN_API_KEY is required for indexing service internal Query API admin calls when ALLOW_UNAUTHENTICATED_ADMIN is false" -}}
{{- end -}}
{{- if and $managedSecret (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") $internalAdminApiKey (or (not $adminApiKey) (ne $internalAdminApiKey $adminApiKey)) -}}
{{- if not .Values.config.SCOPED_API_KEYS -}}
{{- fail "config.INTERNAL_QUERY_API_ADMIN_API_KEY must match config.ADMIN_API_KEY or be present in config.SCOPED_API_KEYS with admin:internal, admin, or * scope" -}}
{{- end -}}
{{- $internalAdminApiKeyAllowed := false -}}
{{- $scopedApiKeys := mustFromJson (include "imposbro-search.scopedApiKeysJson" .) -}}
{{- range $entry := $scopedApiKeys -}}
{{- if eq (toString (get $entry "key")) $internalAdminApiKey -}}
{{- range $scope := (get $entry "scopes") -}}
{{- $scopeName := toString $scope -}}
{{- if or (eq $scopeName "*") (eq $scopeName "admin") (eq $scopeName "admin:internal") -}}
{{- $internalAdminApiKeyAllowed = true -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not $internalAdminApiKeyAllowed -}}
{{- fail "config.INTERNAL_QUERY_API_ADMIN_API_KEY must match config.ADMIN_API_KEY or be present in config.SCOPED_API_KEYS with admin:internal, admin, or * scope" -}}
{{- end -}}
{{- end -}}
{{- $rateLimitEnabled := eq (toString .Values.config.RATE_LIMIT_ENABLED) "true" -}}
{{- if $rateLimitEnabled -}}
{{- $rateLimitBackend := lower (toString .Values.config.RATE_LIMIT_BACKEND) -}}
{{- if and (ne $rateLimitBackend "redis") (ne $rateLimitBackend "memory") -}}
{{- fail "config.RATE_LIMIT_BACKEND must be redis or memory when RATE_LIMIT_ENABLED is true" -}}
{{- end -}}
{{- if lt (int .Values.config.RATE_LIMIT_WINDOW_SECONDS) 1 -}}
{{- fail "config.RATE_LIMIT_WINDOW_SECONDS must be >= 1 when RATE_LIMIT_ENABLED is true" -}}
{{- end -}}
{{- if lt (int .Values.config.RATE_LIMIT_SEARCH_REQUESTS) 1 -}}
{{- fail "config.RATE_LIMIT_SEARCH_REQUESTS must be >= 1 when RATE_LIMIT_ENABLED is true" -}}
{{- end -}}
{{- if lt (int .Values.config.RATE_LIMIT_INGEST_REQUESTS) 1 -}}
{{- fail "config.RATE_LIMIT_INGEST_REQUESTS must be >= 1 when RATE_LIMIT_ENABLED is true" -}}
{{- end -}}
{{- if and (eq $rateLimitBackend "memory") (or .Values.queryApi.autoscaling.enabled (gt (int .Values.queryApi.replicaCount) 1)) -}}
{{- fail "config.RATE_LIMIT_BACKEND=memory is only supported for a single Query API replica; use redis for replicated deployments" -}}
{{- end -}}
{{- end -}}
{{- if lt (int .Values.config.INGEST_BATCH_MAX_DOCUMENTS) 1 -}}
{{- fail "config.INGEST_BATCH_MAX_DOCUMENTS must be >= 1" -}}
{{- end -}}
{{- if $oidcEnabled -}}
{{- $_ := required "config.OIDC_ISSUER is required when OIDC_ENABLED is true" .Values.config.OIDC_ISSUER -}}
{{- $_ := required "config.OIDC_AUDIENCE is required when OIDC_ENABLED is true" .Values.config.OIDC_AUDIENCE -}}
{{- $_ := required "config.OIDC_ALGORITHMS is required when OIDC_ENABLED is true" .Values.config.OIDC_ALGORITHMS -}}
{{- include "imposbro-search.validateSecureOidcUrl" (dict "name" "config.OIDC_ISSUER" "value" .Values.config.OIDC_ISSUER "allowInsecure" $allowInsecureOidcUrls) -}}
{{- include "imposbro-search.validateSecureOidcUrl" (dict "name" "config.OIDC_JWKS_URL" "value" .Values.config.OIDC_JWKS_URL "allowInsecure" $allowInsecureOidcUrls) -}}
{{- if and (not .Values.config.OIDC_JWKS_URL) (not .Values.config.OIDC_PUBLIC_KEY) -}}
{{- fail "config.OIDC_JWKS_URL or config.OIDC_PUBLIC_KEY is required when OIDC_ENABLED is true" -}}
{{- end -}}
{{- if and .Values.config.OIDC_JWKS_URL .Values.config.OIDC_PUBLIC_KEY -}}
{{- fail "config.OIDC_JWKS_URL and config.OIDC_PUBLIC_KEY are mutually exclusive" -}}
{{- end -}}
{{- if contains "HS" (upper (toString .Values.config.OIDC_ALGORITHMS)) -}}
{{- fail "config.OIDC_ALGORITHMS must use asymmetric algorithms; HS* is not allowed" -}}
{{- end -}}
{{- end -}}
{{- $adminUiOidcEnabled := eq (toString .Values.config.ADMIN_UI_OIDC_ENABLED) "true" -}}
{{- $adminUiSecurityProfile := lower (toString .Values.config.ADMIN_UI_SECURITY_PROFILE) -}}
{{- if not (has $adminUiSecurityProfile (list "development" "production" "enterprise")) -}}
{{- fail "config.ADMIN_UI_SECURITY_PROFILE must be development, production, or enterprise" -}}
{{- end -}}
{{- $adminUiPublicOrigin := trimSuffix "/" (toString .Values.config.ADMIN_UI_PUBLIC_ORIGIN) -}}
{{- if and .Values.config.ADMIN_UI_PUBLIC_ORIGIN (ne $adminUiPublicOrigin (toString .Values.config.ADMIN_UI_PUBLIC_ORIGIN)) -}}
{{- fail "config.ADMIN_UI_PUBLIC_ORIGIN must be an exact origin without a trailing slash" -}}
{{- end -}}
{{- if and $adminUiPublicOrigin (not (regexMatch "^https?://([A-Za-z0-9.-]+|\\[::1\\])(:[0-9]{1,5})?$" $adminUiPublicOrigin)) -}}
{{- fail "config.ADMIN_UI_PUBLIC_ORIGIN must be an exact HTTP(S) origin without credentials, path, query, or fragment" -}}
{{- end -}}
{{- $adminUiCredentialMode := lower (toString .Values.config.ADMIN_UI_SERVER_CREDENTIAL_MODE) -}}
{{- if not (has $adminUiCredentialMode (list "disabled" "development" "trusted-header-legacy")) -}}
{{- fail "config.ADMIN_UI_SERVER_CREDENTIAL_MODE must be disabled, development, or trusted-header-legacy" -}}
{{- end -}}
{{- $adminUiLegacyHeaderEnabled := lower (toString .Values.config.ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER) -}}
{{- $adminUiInsecureLocalhost := lower (toString .Values.config.ADMIN_UI_OIDC_ALLOW_INSECURE_LOCALHOST) -}}
{{- $adminUiEnterpriseMode := lower (toString .Values.config.ADMIN_UI_ENTERPRISE_MODE) -}}
{{- range $name, $value := dict "ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER" $adminUiLegacyHeaderEnabled "ADMIN_UI_OIDC_ALLOW_INSECURE_LOCALHOST" $adminUiInsecureLocalhost "ADMIN_UI_ENTERPRISE_MODE" $adminUiEnterpriseMode -}}
{{- if not (has $value (list "true" "false")) -}}
{{- fail (printf "config.%s must be true or false" $name) -}}
{{- end -}}
{{- end -}}
{{- if or (lt (int .Values.config.ADMIN_UI_OIDC_FETCH_TIMEOUT_MS) 250) (gt (int .Values.config.ADMIN_UI_OIDC_FETCH_TIMEOUT_MS) 30000) -}}
{{- fail "config.ADMIN_UI_OIDC_FETCH_TIMEOUT_MS must be between 250 and 30000" -}}
{{- end -}}
{{- $adminUiProductionLike := or (has $profile (list "production" "enterprise")) (has $adminUiSecurityProfile (list "production" "enterprise")) -}}
{{- if and (or $adminUiOidcEnabled $adminUiProductionLike) (not $adminUiPublicOrigin) -}}
{{- fail "config.ADMIN_UI_PUBLIC_ORIGIN is required for OIDC, production, or enterprise Admin UI" -}}
{{- end -}}
{{- if and $adminUiProductionLike $adminUiPublicOrigin (not (hasPrefix "https://" $adminUiPublicOrigin)) -}}
{{- fail "config.ADMIN_UI_PUBLIC_ORIGIN must use https:// in production or enterprise" -}}
{{- end -}}
{{- range $origin := splitList "," (toString .Values.config.ADMIN_UI_OIDC_ALLOWED_ENDPOINT_ORIGINS) -}}
{{- $origin = trim $origin -}}
{{- if $origin -}}
{{- if not (regexMatch "^https://([A-Za-z0-9.-]+|\\[::1\\])(:[0-9]{1,5})?$" $origin) -}}
{{- if not (and (eq $adminUiInsecureLocalhost "true") (not $adminUiProductionLike) (regexMatch "^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?$" $origin)) -}}
{{- fail (printf "config.ADMIN_UI_OIDC_ALLOWED_ENDPOINT_ORIGINS contains an unsafe origin %q" $origin) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $proxyInjectsKeys := or $hasAdminKey $hasDataKey $hasInternalAdminKey .Values.config.INTERNAL_QUERY_API_DATA_API_KEY (has "INTERNAL_QUERY_API_DATA_API_KEY" $declaredSecretKeys) -}}
{{- if and (eq $adminUiCredentialMode "trusted-header-legacy") (ne $adminUiLegacyHeaderEnabled "true") -}}
{{- fail "config.ADMIN_UI_SERVER_CREDENTIAL_MODE=trusted-header-legacy requires ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER=true" -}}
{{- end -}}
{{- if and (eq $adminUiCredentialMode "trusted-header-legacy") $proxyInjectsKeys (not .Values.config.ADMIN_UI_PROXY_TRUSTED_HEADER) -}}
{{- fail "config.ADMIN_UI_PROXY_TRUSTED_HEADER is required when the Admin UI proxy injects server-side API keys" -}}
{{- end -}}
{{- if and (eq $adminUiCredentialMode "trusted-header-legacy") $proxyInjectsKeys .Values.config.ADMIN_UI_PROXY_TRUSTED_HEADER (not .Values.config.ADMIN_UI_PROXY_TRUSTED_VALUE) (not (has "ADMIN_UI_PROXY_TRUSTED_VALUE" $declaredSecretKeys)) -}}
{{- fail "config.ADMIN_UI_PROXY_TRUSTED_VALUE is required when the Admin UI proxy injects server-side API keys" -}}
{{- end -}}
{{- if $adminUiOidcEnabled -}}
{{- if not $oidcEnabled -}}
{{- fail "config.OIDC_ENABLED=true is required when ADMIN_UI_OIDC_ENABLED is true so the Query API can validate Admin UI bearer sessions" -}}
{{- end -}}
{{- $_ := required "config.ADMIN_UI_OIDC_CLIENT_ID is required when ADMIN_UI_OIDC_ENABLED is true" .Values.config.ADMIN_UI_OIDC_CLIENT_ID -}}
{{- if and (not .Values.config.ADMIN_UI_SESSION_SECRET) (not (has "ADMIN_UI_SESSION_SECRET" $declaredSecretKeys)) -}}
{{- fail "config.ADMIN_UI_SESSION_SECRET or declared external Secret key ADMIN_UI_SESSION_SECRET is required when ADMIN_UI_OIDC_ENABLED is true" -}}
{{- end -}}
{{- if and .Values.config.ADMIN_UI_SESSION_SECRET (lt (len (toString .Values.config.ADMIN_UI_SESSION_SECRET)) 32) -}}
{{- fail "config.ADMIN_UI_SESSION_SECRET must be at least 32 characters when ADMIN_UI_OIDC_ENABLED is true" -}}
{{- end -}}
{{- if and (not .Values.config.ADMIN_UI_OIDC_ISSUER) (not (and .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT .Values.config.ADMIN_UI_OIDC_JWKS_URL)) -}}
{{- fail "config.ADMIN_UI_OIDC_ISSUER or explicit Admin UI OIDC authorization, token, and JWKS endpoints are required when ADMIN_UI_OIDC_ENABLED is true" -}}
{{- end -}}
{{- include "imposbro-search.validateAdminUiOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_ISSUER" "value" .Values.config.ADMIN_UI_OIDC_ISSUER "allowInsecureLocalhost" $adminUiInsecureLocalhost) -}}
{{- include "imposbro-search.validateAdminUiOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT" "value" .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT "allowInsecureLocalhost" $adminUiInsecureLocalhost) -}}
{{- include "imposbro-search.validateAdminUiOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT" "value" .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT "allowInsecureLocalhost" $adminUiInsecureLocalhost) -}}
{{- include "imposbro-search.validateAdminUiOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_JWKS_URL" "value" .Values.config.ADMIN_UI_OIDC_JWKS_URL "allowInsecureLocalhost" $adminUiInsecureLocalhost) -}}
{{- if and .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT (not .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT) -}}
{{- fail "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT is required when ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT is set" -}}
{{- end -}}
{{- if and .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT (not .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT) -}}
{{- fail "config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT is required when ADMIN_UI_OIDC_TOKEN_ENDPOINT is set" -}}
{{- end -}}
{{- if and .Values.config.ADMIN_UI_OIDC_JWKS_URL (not (and .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT)) (not .Values.config.ADMIN_UI_OIDC_ISSUER) -}}
{{- fail "config.ADMIN_UI_OIDC_JWKS_URL requires explicit Admin UI OIDC authorization and token endpoints when ADMIN_UI_OIDC_ISSUER is not set" -}}
{{- end -}}
{{- if not (regexMatch "(^|\\s)openid(\\s|$)" (toString .Values.config.ADMIN_UI_OIDC_SCOPES)) -}}
{{- fail "config.ADMIN_UI_OIDC_SCOPES must include openid when ADMIN_UI_OIDC_ENABLED is true" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.indexingService.autoscaling.enabled .Values.indexingService.keda.enabled -}}
{{- fail "indexingService.autoscaling.enabled and indexingService.keda.enabled cannot both be true" -}}
{{- end -}}
{{- if and .Values.queryApi.autoscaling.enabled (not .Values.queryApi.autoscaling.targetCPUUtilizationPercentage) (not .Values.queryApi.autoscaling.targetMemoryUtilizationPercentage) -}}
{{- fail "queryApi.autoscaling requires at least one CPU or memory target" -}}
{{- end -}}
{{- if and .Values.adminUi.autoscaling.enabled (not .Values.adminUi.autoscaling.targetCPUUtilizationPercentage) (not .Values.adminUi.autoscaling.targetMemoryUtilizationPercentage) -}}
{{- fail "adminUi.autoscaling requires at least one CPU or memory target" -}}
{{- end -}}
{{- if and .Values.indexingService.autoscaling.enabled (not .Values.indexingService.autoscaling.targetCPUUtilizationPercentage) (not .Values.indexingService.autoscaling.targetMemoryUtilizationPercentage) -}}
{{- fail "indexingService.autoscaling requires at least one CPU or memory target" -}}
{{- end -}}
{{- if .Values.indexingService.keda.enabled -}}
{{- $_ := required "indexingService.keda.kafka.consumerGroup is required when KEDA is enabled" .Values.indexingService.keda.kafka.consumerGroup -}}
{{- $bootstrap := default .Values.config.KAFKA_BROKER_URL .Values.indexingService.keda.kafka.bootstrapServers -}}
{{- if not $bootstrap -}}
{{- fail "indexingService.keda.kafka.bootstrapServers or config.KAFKA_BROKER_URL is required when KEDA is enabled" -}}
{{- end -}}
{{- if .Values.indexingService.keda.authenticationRef.create -}}
{{- $_ := required "indexingService.keda.authenticationRef.usernameKey is required when creating TriggerAuthentication" .Values.indexingService.keda.authenticationRef.usernameKey -}}
{{- $_ := required "indexingService.keda.authenticationRef.passwordKey is required when creating TriggerAuthentication" .Values.indexingService.keda.authenticationRef.passwordKey -}}
{{- end -}}
{{- end -}}
{{- if .Values.tls.trustBundle.enabled -}}
{{- $_ := required "tls.trustBundle.existingSecret is required when the trust bundle is enabled" .Values.tls.trustBundle.existingSecret -}}
{{- $_ := required "tls.trustBundle.caKey is required when the trust bundle is enabled" .Values.tls.trustBundle.caKey -}}
{{- if not (hasPrefix "/" (toString .Values.tls.trustBundle.mountPath)) -}}
{{- fail "tls.trustBundle.mountPath must be an absolute path" -}}
{{- end -}}
{{- $seenTlsPaths := dict "ca.crt" true -}}
{{- range .Values.tls.trustBundle.additionalKeys -}}
{{- $_ := required "tls.trustBundle.additionalKeys[].key is required" .key -}}
{{- $_ := required "tls.trustBundle.additionalKeys[].path is required" .path -}}
{{- if or (hasPrefix "/" (toString .path)) (contains ".." (toString .path)) -}}
{{- fail "tls.trustBundle.additionalKeys[].path must be a safe relative path" -}}
{{- end -}}
{{- if hasKey $seenTlsPaths (toString .path) -}}
{{- fail (printf "tls.trustBundle.additionalKeys[].path %q is duplicated or reserved" .path) -}}
{{- end -}}
{{- $_ := set $seenTlsPaths (toString .path) true -}}
{{- end -}}
{{- end -}}
{{- if ne (not (not .Values.config.KAFKA_SSL_CERTFILE)) (not (not .Values.config.KAFKA_SSL_KEYFILE)) -}}
{{- fail "config.KAFKA_SSL_CERTFILE and config.KAFKA_SSL_KEYFILE must be configured together" -}}
{{- end -}}
{{- if and (ne $profile "enterprise") .Values.migrations.enabled (ne (lower (toString .Values.config.CONTROL_PLANE_STORE_BACKEND)) "postgres") -}}
{{- fail "migrations.enabled requires config.CONTROL_PLANE_STORE_BACKEND=postgres" -}}
{{- end -}}
{{- if lt (int .Values.migrations.lockTimeoutSeconds) 1 -}}
{{- fail "migrations.lockTimeoutSeconds must be >= 1" -}}
{{- end -}}
{{- if and .Values.availability.podAntiAffinity.enabled (hasKey .Values.affinity "podAntiAffinity") -}}
{{- fail "affinity.podAntiAffinity and availability.podAntiAffinity.enabled cannot both be configured" -}}
{{- end -}}
{{- if and .Values.availability.podAntiAffinity.enabled (not (has (lower (toString .Values.availability.podAntiAffinity.type)) (list "preferred" "required"))) -}}
{{- fail "availability.podAntiAffinity.type must be preferred or required" -}}
{{- end -}}
{{- if .Values.availability.podAntiAffinity.enabled -}}
{{- $_ := required "availability.podAntiAffinity.topologyKey is required" .Values.availability.podAntiAffinity.topologyKey -}}
{{- end -}}
{{- range $name, $workload := dict "queryApi" .Values.queryApi "adminUi" .Values.adminUi "indexingService" .Values.indexingService -}}
{{- if and $workload.autoscaling.enabled (ge (int $workload.autoscaling.minReplicas) (int $workload.autoscaling.maxReplicas)) -}}
{{- fail (printf "%s.autoscaling.maxReplicas must be greater than minReplicas" $name) -}}
{{- end -}}
{{- if and $workload.podDisruptionBudget.enabled $workload.podDisruptionBudget.minAvailable $workload.podDisruptionBudget.maxUnavailable -}}
{{- fail (printf "%s.podDisruptionBudget must set only one of minAvailable or maxUnavailable" $name) -}}
{{- end -}}
{{- end -}}

{{/* The enterprise profile is intentionally opinionated and fail-closed. */}}
{{- if eq $profile "enterprise" -}}
{{- if ne $otelSdkDisabled "false" -}}
{{- fail "enterprise profile requires config.OTEL_SDK_DISABLED=false" -}}
{{- end -}}
{{- if ne $otelExporter "otlp" -}}
{{- fail "enterprise profile requires config.OTEL_TRACES_EXPORTER=otlp" -}}
{{- end -}}
{{- if or (not (hasPrefix "https://" $otelEndpoint)) (contains "@" $otelEndpoint) -}}
{{- fail "enterprise profile requires a credential-free HTTPS OTLP trace endpoint" -}}
{{- end -}}
{{- if or (eq $otelSampler "always_off") (le (float64 .Values.config.OTEL_TRACES_SAMPLER_ARG) 0.0) -}}
{{- fail "enterprise profile requires a non-zero OpenTelemetry sampling policy" -}}
{{- end -}}
{{- range $name, $value := dict "OTEL_DEPLOYMENT_ENVIRONMENT" .Values.config.OTEL_DEPLOYMENT_ENVIRONMENT "OTEL_BUILD_ID" .Values.config.OTEL_BUILD_ID "OTEL_SERVICE_REVISION" .Values.config.OTEL_SERVICE_REVISION -}}
{{- if or (not $value) (eq (lower (toString $value)) "unknown") (eq (lower (toString $value)) "development") (not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}$" (toString $value))) -}}
{{- fail (printf "enterprise profile requires release-safe config.%s" $name) -}}
{{- end -}}
{{- end -}}
{{- if ne $adminUiSecurityProfile "enterprise" -}}
{{- fail "enterprise profile requires config.ADMIN_UI_SECURITY_PROFILE=enterprise" -}}
{{- end -}}
{{- if or (not $adminUiPublicOrigin) (not (regexMatch "^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$" $adminUiPublicOrigin)) -}}
{{- fail "enterprise profile requires an exact HTTPS config.ADMIN_UI_PUBLIC_ORIGIN" -}}
{{- end -}}
{{- if ne $adminUiCredentialMode "disabled" -}}
{{- fail "enterprise profile requires config.ADMIN_UI_SERVER_CREDENTIAL_MODE=disabled" -}}
{{- end -}}
{{- if eq $adminUiLegacyHeaderEnabled "true" -}}
{{- fail "enterprise profile forbids config.ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER=true" -}}
{{- end -}}
{{- if eq $adminUiInsecureLocalhost "true" -}}
{{- fail "enterprise profile forbids config.ADMIN_UI_OIDC_ALLOW_INSECURE_LOCALHOST=true" -}}
{{- end -}}
{{- if $managedSecret -}}
{{- fail "enterprise profile forbids config.useSecret inline values; use secrets.existingSecret or secrets.externalSecret" -}}
{{- end -}}
{{- $_ := required "secrets.rolloutVersion is required in enterprise profile for deterministic secret rotation rollouts" .Values.secrets.rolloutVersion -}}
{{- $sensitiveValues := dict
  "CONTROL_PLANE_DATABASE_URL" .Values.config.CONTROL_PLANE_DATABASE_URL
  "REDIS_URL" .Values.config.REDIS_URL
  "INTERNAL_STATE_API_KEY" .Values.config.INTERNAL_STATE_API_KEY
  "DEFAULT_DATA_CLUSTER_API_KEY" .Values.config.DEFAULT_DATA_CLUSTER_API_KEY
  "DEFAULT_DATA2_CLUSTER_API_KEY" .Values.config.DEFAULT_DATA2_CLUSTER_API_KEY
  "ADMIN_API_KEY" .Values.config.ADMIN_API_KEY
  "DATA_API_KEY" .Values.config.DATA_API_KEY
  "INTERNAL_QUERY_API_ADMIN_API_KEY" .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY
  "INTERNAL_QUERY_API_DATA_API_KEY" .Values.config.INTERNAL_QUERY_API_DATA_API_KEY
  "SCOPED_API_KEYS" .Values.config.SCOPED_API_KEYS
  "KAFKA_SASL_USERNAME" .Values.config.KAFKA_SASL_USERNAME
  "KAFKA_SASL_PASSWORD" .Values.config.KAFKA_SASL_PASSWORD
  "OTEL_EXPORTER_OTLP_HEADERS" .Values.config.OTEL_EXPORTER_OTLP_HEADERS
  "ADMIN_UI_PROXY_TRUSTED_VALUE" .Values.config.ADMIN_UI_PROXY_TRUSTED_VALUE
  "ADMIN_UI_OIDC_CLIENT_SECRET" .Values.config.ADMIN_UI_OIDC_CLIENT_SECRET
  "ADMIN_UI_SESSION_SECRET" .Values.config.ADMIN_UI_SESSION_SECRET -}}
{{- range $name, $value := $sensitiveValues -}}
{{- if $value -}}
{{- fail (printf "enterprise profile forbids plaintext config.%s; provide it through the selected external Secret" $name) -}}
{{- end -}}
{{- end -}}
{{- range $requiredKey := list "CONTROL_PLANE_DATABASE_URL" "REDIS_URL" "INTERNAL_STATE_API_KEY" "DEFAULT_DATA_CLUSTER_API_KEY" "ADMIN_API_KEY" "INTERNAL_QUERY_API_ADMIN_API_KEY" "KAFKA_SASL_USERNAME" "KAFKA_SASL_PASSWORD" "ADMIN_UI_SESSION_SECRET" -}}
{{- if not (has $requiredKey $declaredSecretKeys) -}}
{{- fail (printf "enterprise profile requires secrets.providedKeys to declare %s" $requiredKey) -}}
{{- end -}}
{{- end -}}
{{- $workloadRequirements := dict
  "migration" (list "CONTROL_PLANE_DATABASE_URL")
  "queryApi" (list "CONTROL_PLANE_DATABASE_URL" "REDIS_URL" "ADMIN_API_KEY" "KAFKA_SASL_USERNAME" "KAFKA_SASL_PASSWORD")
  "indexingService" (list "CONTROL_PLANE_DATABASE_URL" "INTERNAL_QUERY_API_ADMIN_API_KEY" "KAFKA_SASL_USERNAME" "KAFKA_SASL_PASSWORD")
  "adminUi" (list "ADMIN_UI_SESSION_SECRET") -}}
{{- range $workload, $requiredKeys := $workloadRequirements -}}
{{- $configuredKeys := get $.Values.secrets.workloadKeys $workload -}}
{{- range $requiredKey := $requiredKeys -}}
{{- if not (has $requiredKey $configuredKeys) -}}
{{- fail (printf "enterprise profile requires secrets.workloadKeys.%s to include %s" $workload $requiredKey) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $name, $ref := dict "INTERNAL_STATE_API_KEY_REF" .Values.config.INTERNAL_STATE_API_KEY_REF "DEFAULT_DATA_CLUSTER_API_KEY_REF" .Values.config.DEFAULT_DATA_CLUSTER_API_KEY_REF -}}
{{- if not (hasPrefix "env:" (toString $ref)) -}}
{{- fail (printf "enterprise profile requires config.%s=env:<runtime-variable>" $name) -}}
{{- end -}}
{{- if not (regexMatch "^env:[A-Z][A-Z0-9_]{0,127}$" (toString $ref)) -}}
{{- fail (printf "enterprise config.%s has an invalid environment reference" $name) -}}
{{- end -}}
{{- end -}}
{{- if and .Values.config.DEFAULT_DATA2_CLUSTER_NODES (not (hasPrefix "env:" (toString .Values.config.DEFAULT_DATA2_CLUSTER_API_KEY_REF))) -}}
{{- fail "enterprise profile requires config.DEFAULT_DATA2_CLUSTER_API_KEY_REF=env:<runtime-variable> when the secondary cluster is configured" -}}
{{- end -}}
{{- $queryKeys := .Values.secrets.workloadKeys.queryApi -}}
{{- range $forbiddenKey := list "INTERNAL_QUERY_API_ADMIN_API_KEY" "INTERNAL_QUERY_API_DATA_API_KEY" "ADMIN_UI_PROXY_TRUSTED_VALUE" "ADMIN_UI_OIDC_CLIENT_SECRET" "ADMIN_UI_SESSION_SECRET" -}}
{{- if has $forbiddenKey $queryKeys -}}
{{- fail (printf "enterprise Query API secret scope forbids %s" $forbiddenKey) -}}
{{- end -}}
{{- end -}}
{{- $workerKeys := .Values.secrets.workloadKeys.indexingService -}}
{{- range $forbiddenKey := list "REDIS_URL" "INTERNAL_STATE_API_KEY" "DEFAULT_DATA_CLUSTER_API_KEY" "DEFAULT_DATA2_CLUSTER_API_KEY" "ADMIN_API_KEY" "DATA_API_KEY" "SCOPED_API_KEYS" "INTERNAL_QUERY_API_DATA_API_KEY" "ADMIN_UI_PROXY_TRUSTED_VALUE" "ADMIN_UI_OIDC_CLIENT_SECRET" "ADMIN_UI_SESSION_SECRET" -}}
{{- if has $forbiddenKey $workerKeys -}}
{{- fail (printf "enterprise indexing service secret scope forbids %s" $forbiddenKey) -}}
{{- end -}}
{{- end -}}
{{- $adminKeys := .Values.secrets.workloadKeys.adminUi -}}
{{- range $forbiddenKey := list "CONTROL_PLANE_DATABASE_URL" "REDIS_URL" "INTERNAL_STATE_API_KEY" "DEFAULT_DATA_CLUSTER_API_KEY" "DEFAULT_DATA2_CLUSTER_API_KEY" "ADMIN_API_KEY" "DATA_API_KEY" "SCOPED_API_KEYS" "INTERNAL_QUERY_API_ADMIN_API_KEY" "INTERNAL_QUERY_API_DATA_API_KEY" "KAFKA_SASL_USERNAME" "KAFKA_SASL_PASSWORD" "OTEL_EXPORTER_OTLP_HEADERS" -}}
{{- if has $forbiddenKey $adminKeys -}}
{{- fail (printf "enterprise Admin UI secret scope forbids %s" $forbiddenKey) -}}
{{- end -}}
{{- end -}}
{{- if ne (len .Values.secrets.workloadKeys.migration) 1 -}}
{{- fail "enterprise migration secret scope must contain only CONTROL_PLANE_DATABASE_URL" -}}
{{- end -}}
{{- if $externalSecret -}}
{{- if not .Values.secrets.externalSecret.data -}}
{{- fail "enterprise profile requires explicit secrets.externalSecret.data mappings" -}}
{{- end -}}
{{- $externalDataKeys := list -}}
{{- range .Values.secrets.externalSecret.data -}}
{{- $externalDataKeys = append $externalDataKeys (toString .secretKey) -}}
{{- end -}}
{{- range $requiredKey := list "CONTROL_PLANE_DATABASE_URL" "REDIS_URL" "INTERNAL_STATE_API_KEY" "DEFAULT_DATA_CLUSTER_API_KEY" "ADMIN_API_KEY" "INTERNAL_QUERY_API_ADMIN_API_KEY" "KAFKA_SASL_USERNAME" "KAFKA_SASL_PASSWORD" "ADMIN_UI_SESSION_SECRET" -}}
{{- if not (has $requiredKey $externalDataKeys) -}}
{{- fail (printf "enterprise ExternalSecret must explicitly map %s" $requiredKey) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ne (lower (toString .Values.config.CONTROL_PLANE_STORE_BACKEND)) "postgres" -}}
{{- fail "enterprise profile requires config.CONTROL_PLANE_STORE_BACKEND=postgres" -}}
{{- end -}}
{{- if ne $indexingEventStoreBackend "postgres" -}}
{{- fail "enterprise profile requires config.INDEXING_EVENT_STORE_BACKEND=postgres" -}}
{{- end -}}
{{- if ne (lower (toString .Values.config.PGSSLMODE)) "verify-full" -}}
{{- fail "enterprise profile requires config.PGSSLMODE=verify-full" -}}
{{- end -}}
{{- if not .Values.migrations.enabled -}}
{{- fail "enterprise profile requires migrations.enabled=true" -}}
{{- end -}}
{{- if not .Values.tls.trustBundle.enabled -}}
{{- fail "enterprise profile requires tls.trustBundle.enabled=true" -}}
{{- end -}}
{{- $caPath := printf "%s/ca.crt" (trimSuffix "/" (toString .Values.tls.trustBundle.mountPath)) -}}
{{- if ne (toString .Values.config.PGSSLROOTCERT) $caPath -}}
{{- fail (printf "enterprise profile requires config.PGSSLROOTCERT=%s" $caPath) -}}
{{- end -}}
{{- if ne (toString .Values.config.KAFKA_SSL_CAFILE) $caPath -}}
{{- fail (printf "enterprise profile requires config.KAFKA_SSL_CAFILE=%s" $caPath) -}}
{{- end -}}
{{- if ne $internalStateProtocol "https" -}}
{{- fail "enterprise profile requires config.INTERNAL_STATE_PROTOCOL=https" -}}
{{- end -}}
{{- if ne $defaultDataProtocol "https" -}}
{{- fail "enterprise profile requires config.DEFAULT_DATA_CLUSTER_PROTOCOL=https" -}}
{{- end -}}
{{- if and .Values.config.DEFAULT_DATA2_CLUSTER_NODES (ne $defaultData2Protocol "https") -}}
{{- fail "enterprise profile requires config.DEFAULT_DATA2_CLUSTER_PROTOCOL=https when the secondary cluster is configured" -}}
{{- end -}}
{{- if ne $kafkaSecurityProtocol "SASL_SSL" -}}
{{- fail "enterprise profile requires config.KAFKA_SECURITY_PROTOCOL=SASL_SSL" -}}
{{- end -}}
{{- if not (eq (toString .Values.config.KAFKA_SSL_CHECK_HOSTNAME) "true") -}}
{{- fail "enterprise profile requires config.KAFKA_SSL_CHECK_HOSTNAME=true" -}}
{{- end -}}
{{- if contains "@" (toString .Values.config.KAFKA_BROKER_URL) -}}
{{- fail "enterprise profile forbids credentials in config.KAFKA_BROKER_URL" -}}
{{- end -}}
{{- if not $oidcEnabled -}}
{{- fail "enterprise profile requires config.OIDC_ENABLED=true" -}}
{{- end -}}
{{- if not $adminUiOidcEnabled -}}
{{- fail "enterprise profile requires config.ADMIN_UI_OIDC_ENABLED=true" -}}
{{- end -}}
{{- $_ := required "enterprise profile requires config.ADMIN_UI_OIDC_REDIRECT_URI" .Values.config.ADMIN_UI_OIDC_REDIRECT_URI -}}
{{- if not (hasPrefix "https://" (toString .Values.config.ADMIN_UI_OIDC_REDIRECT_URI)) -}}
{{- fail "enterprise profile requires an HTTPS config.ADMIN_UI_OIDC_REDIRECT_URI" -}}
{{- end -}}
{{- if ne (toString .Values.config.ADMIN_UI_OIDC_REDIRECT_URI) (printf "%s/api/auth/callback" $adminUiPublicOrigin) -}}
{{- fail "enterprise profile requires config.ADMIN_UI_OIDC_REDIRECT_URI to equal ADMIN_UI_PUBLIC_ORIGIN/api/auth/callback" -}}
{{- end -}}
{{- if $allowInsecureOidcUrls -}}
{{- fail "enterprise profile forbids config.ALLOW_INSECURE_OIDC_URLS=true" -}}
{{- end -}}
{{- if or (ne (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") (ne (toString .Values.config.ALLOW_UNAUTHENTICATED_DATA) "false") -}}
{{- fail "enterprise profile requires unauthenticated admin and data access to be disabled" -}}
{{- end -}}
{{- if ne (toString .Values.config.AUTHZ_API_KEY_TENANT_BYPASS) "false" -}}
{{- fail "enterprise profile requires config.AUTHZ_API_KEY_TENANT_BYPASS=false" -}}
{{- end -}}
{{- if ne (toString .Values.config.AUTHZ_REQUIRE_COLLECTION_POLICY) "true" -}}
{{- fail "enterprise profile requires config.AUTHZ_REQUIRE_COLLECTION_POLICY=true" -}}
{{- end -}}
{{- $_ := required "enterprise profile requires config.AUTHZ_COLLECTION_POLICIES" .Values.config.AUTHZ_COLLECTION_POLICIES -}}
{{- if ne (toString .Values.config.AUDIT_LOG_ENABLED) "true" -}}
{{- fail "enterprise profile requires config.AUDIT_LOG_ENABLED=true" -}}
{{- end -}}
{{- if lt (int .Values.config.AUDIT_LOG_MAX_RESULTS) 1 -}}
{{- fail "enterprise profile requires config.AUDIT_LOG_MAX_RESULTS >= 1" -}}
{{- end -}}
{{- if or (ne (toString .Values.config.RATE_LIMIT_ENABLED) "true") (ne (lower (toString .Values.config.RATE_LIMIT_BACKEND)) "redis") (ne (toString .Values.config.RATE_LIMIT_FAIL_CLOSED) "true") -}}
{{- fail "enterprise profile requires Redis rate limiting with RATE_LIMIT_ENABLED=true and RATE_LIMIT_FAIL_CLOSED=true" -}}
{{- end -}}
{{- if ne $readinessPolicy "strict" -}}
{{- fail "enterprise profile requires config.READINESS_POLICY=strict" -}}
{{- end -}}
{{- $cors := trim (toString .Values.config.CORS_ORIGINS) -}}
{{- if not $cors -}}
{{- fail "enterprise profile requires at least one exact HTTPS config.CORS_ORIGINS origin" -}}
{{- end -}}
{{- range $origin := splitList "," $cors -}}
{{- $origin = trim $origin -}}
{{- if or (eq $origin "*") (not (regexMatch "^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$" $origin)) -}}
{{- fail (printf "enterprise CORS origins must be exact HTTPS origins without wildcards or paths (got %q)" $origin) -}}
{{- end -}}
{{- end -}}
{{- if not .Values.networkPolicy.enabled -}}
{{- fail "enterprise profile requires networkPolicy.enabled=true" -}}
{{- end -}}
{{- if or (not .Values.networkPolicy.queryApi.egress) (not .Values.networkPolicy.adminUi.egress) (not .Values.networkPolicy.indexingService.egress) -}}
{{- fail "enterprise profile requires explicit egress rules for queryApi, adminUi, and indexingService" -}}
{{- end -}}
{{- if or (not .Values.networkPolicy.queryApi.health.from) (not .Values.networkPolicy.adminUi.health.from) (not .Values.networkPolicy.indexingService.health.ingress.from) -}}
{{- fail "enterprise profile requires explicit kubelet/CNI health-probe sources for every NetworkPolicy" -}}
{{- end -}}
{{- if and .Values.queryApi.ingress.enabled (not .Values.queryApi.ingress.tls) -}}
{{- fail "enterprise profile requires queryApi.ingress.tls when ingress is enabled" -}}
{{- end -}}
{{- if and .Values.adminUi.ingress.enabled (not .Values.adminUi.ingress.tls) -}}
{{- fail "enterprise profile requires adminUi.ingress.tls when ingress is enabled" -}}
{{- end -}}
{{- if or (ne .Values.queryApi.service.type "ClusterIP") (ne .Values.adminUi.service.type "ClusterIP") -}}
{{- fail "enterprise profile requires Query API and Admin UI services to remain ClusterIP behind an authenticated TLS edge" -}}
{{- end -}}
{{- if or (not .Values.queryApi.podDisruptionBudget.enabled) (not .Values.adminUi.podDisruptionBudget.enabled) (not .Values.indexingService.podDisruptionBudget.enabled) -}}
{{- fail "enterprise profile requires PodDisruptionBudgets for every workload" -}}
{{- end -}}
{{- range $name, $workload := dict "queryApi" .Values.queryApi "adminUi" .Values.adminUi "indexingService" .Values.indexingService -}}
{{- $minimumReplicas := int $workload.replicaCount -}}
{{- if $workload.autoscaling.enabled -}}
{{- $minimumReplicas = int $workload.autoscaling.minReplicas -}}
{{- end -}}
{{- if and (eq $name "indexingService") $workload.keda.enabled -}}
{{- $minimumReplicas = int $workload.keda.minReplicaCount -}}
{{- end -}}
{{- if lt $minimumReplicas 2 -}}
{{- fail (printf "enterprise profile requires at least two %s replicas" $name) -}}
{{- end -}}
{{- if not $workload.topologySpreadConstraints -}}
{{- fail (printf "enterprise profile requires %s.topologySpreadConstraints" $name) -}}
{{- end -}}
{{- $component := get (dict "queryApi" "query-api" "adminUi" "admin-ui" "indexingService" "indexing-service") $name -}}
{{- range $constraint := $workload.topologySpreadConstraints -}}
{{- if or (ne (toString $constraint.whenUnsatisfiable) "DoNotSchedule") (ne (int $constraint.maxSkew) 1) (not $constraint.topologyKey) -}}
{{- fail (printf "enterprise %s topology spread constraints require maxSkew=1, a topologyKey, and whenUnsatisfiable=DoNotSchedule" $name) -}}
{{- end -}}
{{- if ne (dig "labelSelector" "matchLabels" "app.kubernetes.io/component" "" $constraint) $component -}}
{{- fail (printf "enterprise %s topology spread selector must target app.kubernetes.io/component=%s" $name $component) -}}
{{- end -}}
{{- end -}}
{{- if or (ne (toString $workload.strategy.type) "RollingUpdate") (ne (toString $workload.strategy.rollingUpdate.maxUnavailable) "0") -}}
{{- fail (printf "enterprise %s requires RollingUpdate with maxUnavailable=0" $name) -}}
{{- end -}}
{{- end -}}
{{- if not .Values.availability.podAntiAffinity.enabled -}}
{{- fail "enterprise profile requires availability.podAntiAffinity.enabled=true" -}}
{{- end -}}
{{- if or (not .Values.podSecurityContext.runAsNonRoot) (ne (toString .Values.podSecurityContext.seccompProfile.type) "RuntimeDefault") -}}
{{- fail "enterprise profile requires podSecurityContext.runAsNonRoot=true and seccompProfile.type=RuntimeDefault" -}}
{{- end -}}
{{- if or (not .Values.securityContext.runAsNonRoot) .Values.securityContext.allowPrivilegeEscalation .Values.securityContext.privileged (not .Values.securityContext.readOnlyRootFilesystem) (not (has "ALL" .Values.securityContext.capabilities.drop)) -}}
{{- fail "enterprise profile requires a non-root, non-privileged, read-only container security context with all capabilities dropped" -}}
{{- end -}}
{{- if .Values.serviceAccount.automountServiceAccountToken -}}
{{- fail "enterprise profile requires serviceAccount.automountServiceAccountToken=false" -}}
{{- end -}}
{{- if and (not .Values.serviceAccount.create) (or (not .Values.serviceAccount.name) (eq .Values.serviceAccount.name "default")) -}}
{{- fail "enterprise profile requires a dedicated non-default service account" -}}
{{- end -}}
{{- if not .Values.writableTmpDir.enabled -}}
{{- fail "enterprise profile requires writableTmpDir.enabled=true with a read-only root filesystem" -}}
{{- end -}}
{{- if and .Values.indexingService.keda.enabled (not (or .Values.indexingService.keda.authenticationRef.create .Values.indexingService.keda.authenticationRef.name)) -}}
{{- fail "enterprise Kafka KEDA scaling requires a created or existing indexingService.keda.authenticationRef" -}}
{{- end -}}
{{- if and .Values.indexingService.keda.enabled .Values.indexingService.keda.authenticationRef.create (not .Values.indexingService.keda.authenticationRef.caKey) -}}
{{- fail "enterprise created KEDA TriggerAuthentication requires indexingService.keda.authenticationRef.caKey" -}}
{{- end -}}
{{- if .Values.indexingService.keda.enabled -}}
{{- $expectedKedaSasl := get (dict "PLAIN" "plaintext" "SCRAM-SHA-256" "scram_sha256" "SCRAM-SHA-512" "scram_sha512") (upper (toString .Values.config.KAFKA_SASL_MECHANISM)) -}}
{{- if ne (lower (toString .Values.indexingService.keda.kafka.sasl)) $expectedKedaSasl -}}
{{- fail (printf "enterprise KEDA Kafka sasl must match runtime mechanism (%s)" $expectedKedaSasl) -}}
{{- end -}}
{{- if or (ne (lower (toString .Values.indexingService.keda.kafka.tls)) "enable") (eq (lower (toString .Values.indexingService.keda.kafka.unsafeSsl)) "true") -}}
{{- fail "enterprise KEDA Kafka scaling requires tls=enable and unsafeSsl=false" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
