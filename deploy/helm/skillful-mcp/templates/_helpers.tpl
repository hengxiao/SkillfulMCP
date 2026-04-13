{{/*
Common helpers. Kept small — this chart does not aspire to match Bitnami's
scope.
*/}}

{{- define "skillful-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "skillful-mcp.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "skillful-mcp.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "skillful-mcp.catalog.fullname" -}}
{{- printf "%s-catalog" (include "skillful-mcp.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "skillful-mcp.webui.fullname" -}}
{{- printf "%s-webui" (include "skillful-mcp.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "skillful-mcp.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "skillful-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "skillful-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "skillful-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}
