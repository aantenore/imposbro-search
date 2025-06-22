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
