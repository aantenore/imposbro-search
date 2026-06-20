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
{{- $_ := required "config.KAFKA_BROKER_URL is required; deploy Kafka separately and provide its bootstrap URL" .Values.config.KAFKA_BROKER_URL -}}
{{- $_ := required "config.REDIS_URL is required; deploy Redis separately and provide its URL" .Values.config.REDIS_URL -}}
{{- $_ := required "config.INTERNAL_STATE_NODES is required; deploy Typesense state nodes separately and provide hostnames" .Values.config.INTERNAL_STATE_NODES -}}
{{- $_ := required "config.DEFAULT_DATA_CLUSTER_NODES is required; deploy Typesense data nodes separately and provide hostnames" .Values.config.DEFAULT_DATA_CLUSTER_NODES -}}
{{- $_ := required "config.INTERNAL_QUERY_API_URL is required for Admin UI and indexing service discovery" .Values.config.INTERNAL_QUERY_API_URL -}}
{{- $_ := required "config.REQUEST_ID_HEADER is required for HTTP/Kafka request correlation" .Values.config.REQUEST_ID_HEADER -}}
{{- if not .Values.config.useSecret -}}
{{- fail "config.useSecret must be true unless you customize the chart to inject required API keys from an external secret manager" -}}
{{- end -}}
{{- $_ := required "config.INTERNAL_STATE_API_KEY is required" .Values.config.INTERNAL_STATE_API_KEY -}}
{{- $_ := required "config.DEFAULT_DATA_CLUSTER_API_KEY is required" .Values.config.DEFAULT_DATA_CLUSTER_API_KEY -}}
{{- $oidcEnabled := eq (toString .Values.config.OIDC_ENABLED) "true" -}}
{{- $allowInsecureOidcUrls := eq (toString .Values.config.ALLOW_INSECURE_OIDC_URLS) "true" -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") (not .Values.config.ADMIN_API_KEY) (not .Values.config.SCOPED_API_KEYS) (not $oidcEnabled) -}}
{{- fail "config.ADMIN_API_KEY, config.SCOPED_API_KEYS, or config.OIDC_ENABLED=true is required when ALLOW_UNAUTHENTICATED_ADMIN is false" -}}
{{- end -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_DATA) "false") (not .Values.config.DATA_API_KEY) (not .Values.config.SCOPED_API_KEYS) (not $oidcEnabled) -}}
{{- fail "config.DATA_API_KEY, config.SCOPED_API_KEYS, or config.OIDC_ENABLED=true is required when ALLOW_UNAUTHENTICATED_DATA is false" -}}
{{- end -}}
{{- $adminApiKey := toString .Values.config.ADMIN_API_KEY -}}
{{- $internalAdminApiKey := toString .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") (not $adminApiKey) (not $internalAdminApiKey) -}}
{{- fail "config.ADMIN_API_KEY or config.INTERNAL_QUERY_API_ADMIN_API_KEY is required for indexing service internal Query API admin calls when ALLOW_UNAUTHENTICATED_ADMIN is false" -}}
{{- end -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") $internalAdminApiKey (or (not $adminApiKey) (ne $internalAdminApiKey $adminApiKey)) -}}
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
{{- if and (not $adminUiOidcEnabled) (or .Values.config.ADMIN_API_KEY .Values.config.DATA_API_KEY .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY .Values.config.INTERNAL_QUERY_API_DATA_API_KEY) (not .Values.config.ADMIN_UI_PROXY_TRUSTED_HEADER) -}}
{{- fail "config.ADMIN_UI_PROXY_TRUSTED_HEADER is required when the Admin UI proxy injects server-side API keys" -}}
{{- end -}}
{{- if and (not $adminUiOidcEnabled) (or .Values.config.ADMIN_API_KEY .Values.config.DATA_API_KEY .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY .Values.config.INTERNAL_QUERY_API_DATA_API_KEY) .Values.config.ADMIN_UI_PROXY_TRUSTED_HEADER (not .Values.config.ADMIN_UI_PROXY_TRUSTED_VALUE) -}}
{{- fail "config.ADMIN_UI_PROXY_TRUSTED_VALUE is required when the Admin UI proxy injects server-side API keys" -}}
{{- end -}}
{{- if $adminUiOidcEnabled -}}
{{- if not $oidcEnabled -}}
{{- fail "config.OIDC_ENABLED=true is required when ADMIN_UI_OIDC_ENABLED is true so the Query API can validate Admin UI bearer sessions" -}}
{{- end -}}
{{- $_ := required "config.ADMIN_UI_OIDC_CLIENT_ID is required when ADMIN_UI_OIDC_ENABLED is true" .Values.config.ADMIN_UI_OIDC_CLIENT_ID -}}
{{- $_ := required "config.ADMIN_UI_SESSION_SECRET is required when ADMIN_UI_OIDC_ENABLED is true" .Values.config.ADMIN_UI_SESSION_SECRET -}}
{{- if lt (len (toString .Values.config.ADMIN_UI_SESSION_SECRET)) 32 -}}
{{- fail "config.ADMIN_UI_SESSION_SECRET must be at least 32 characters when ADMIN_UI_OIDC_ENABLED is true" -}}
{{- end -}}
{{- if and (not .Values.config.ADMIN_UI_OIDC_ISSUER) (not (and .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT .Values.config.ADMIN_UI_OIDC_JWKS_URL)) -}}
{{- fail "config.ADMIN_UI_OIDC_ISSUER or explicit Admin UI OIDC authorization, token, and JWKS endpoints are required when ADMIN_UI_OIDC_ENABLED is true" -}}
{{- end -}}
{{- include "imposbro-search.validateSecureOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_ISSUER" "value" .Values.config.ADMIN_UI_OIDC_ISSUER "allowInsecure" $allowInsecureOidcUrls) -}}
{{- include "imposbro-search.validateSecureOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT" "value" .Values.config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT "allowInsecure" $allowInsecureOidcUrls) -}}
{{- include "imposbro-search.validateSecureOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT" "value" .Values.config.ADMIN_UI_OIDC_TOKEN_ENDPOINT "allowInsecure" $allowInsecureOidcUrls) -}}
{{- include "imposbro-search.validateSecureOidcUrl" (dict "name" "config.ADMIN_UI_OIDC_JWKS_URL" "value" .Values.config.ADMIN_UI_OIDC_JWKS_URL "allowInsecure" $allowInsecureOidcUrls) -}}
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
{{- end -}}
{{- end }}
