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
{{- if or (contains "your-registry/" $value) (hasSuffix ":latest" $value) -}}
{{- fail (printf "%s must be a non-placeholder image and must not use the mutable :latest tag (got %q)" $name $value) -}}
{{- end -}}
{{- $value -}}
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
{{- if not .Values.config.useSecret -}}
{{- fail "config.useSecret must be true unless you customize the chart to inject required API keys from an external secret manager" -}}
{{- end -}}
{{- $_ := required "config.INTERNAL_STATE_API_KEY is required" .Values.config.INTERNAL_STATE_API_KEY -}}
{{- $_ := required "config.DEFAULT_DATA_CLUSTER_API_KEY is required" .Values.config.DEFAULT_DATA_CLUSTER_API_KEY -}}
{{- $oidcEnabled := eq (toString .Values.config.OIDC_ENABLED) "true" -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_ADMIN) "false") (not .Values.config.ADMIN_API_KEY) (not .Values.config.SCOPED_API_KEYS) (not $oidcEnabled) -}}
{{- fail "config.ADMIN_API_KEY, config.SCOPED_API_KEYS, or config.OIDC_ENABLED=true is required when ALLOW_UNAUTHENTICATED_ADMIN is false" -}}
{{- end -}}
{{- if and (eq (toString .Values.config.ALLOW_UNAUTHENTICATED_DATA) "false") (not .Values.config.DATA_API_KEY) (not .Values.config.SCOPED_API_KEYS) (not $oidcEnabled) -}}
{{- fail "config.DATA_API_KEY, config.SCOPED_API_KEYS, or config.OIDC_ENABLED=true is required when ALLOW_UNAUTHENTICATED_DATA is false" -}}
{{- end -}}
{{- if $oidcEnabled -}}
{{- $_ := required "config.OIDC_ISSUER is required when OIDC_ENABLED is true" .Values.config.OIDC_ISSUER -}}
{{- $_ := required "config.OIDC_AUDIENCE is required when OIDC_ENABLED is true" .Values.config.OIDC_AUDIENCE -}}
{{- $_ := required "config.OIDC_ALGORITHMS is required when OIDC_ENABLED is true" .Values.config.OIDC_ALGORITHMS -}}
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
{{- if and (or .Values.config.ADMIN_API_KEY .Values.config.DATA_API_KEY .Values.config.INTERNAL_QUERY_API_ADMIN_API_KEY .Values.config.INTERNAL_QUERY_API_DATA_API_KEY) (not .Values.config.ADMIN_UI_PROXY_TRUSTED_HEADER) -}}
{{- fail "config.ADMIN_UI_PROXY_TRUSTED_HEADER is required when the Admin UI proxy injects server-side API keys" -}}
{{- end -}}
{{- end }}
