{{- define "llama.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "llama.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "llama.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "llama.labels" -}}
app.kubernetes.io/name: {{ include "llama.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "llama.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llama.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "llama.hfSecretName" -}}
{{- if .Values.hfToken.value -}}
{{ include "llama.fullname" . }}-hf-token
{{- else -}}
{{ .Values.hfToken.existingSecret }}
{{- end -}}
{{- end -}}
