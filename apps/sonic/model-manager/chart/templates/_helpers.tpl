{{/*
Instance name (equal to release name unless overridden)
*/}}
{{- define "modelManager.name" -}}
{{- if .Values.nameOverride }}
  {{- printf "%s" .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- else }}
  {{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end -}}

{{/*
Standard labels, matching SuperSONIC's conventions
*/}}
{{- define "modelManager.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ include "modelManager.name" . }}
app.kubernetes.io/component: model-manager
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "modelManager.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ include "modelManager.name" . }}
app.kubernetes.io/component: model-manager
{{- end -}}

{{- define "modelManager.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
  {{- default (include "modelManager.name" .) .Values.serviceAccount.name -}}
{{- else -}}
  {{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Label selector used to find Triton pods. Derived from the SuperSONIC release
name when not set explicitly.
*/}}
{{- define "modelManager.tritonSelector" -}}
{{- if .Values.triton.labelSelector -}}
  {{- .Values.triton.labelSelector -}}
{{- else if .Values.supersonicRelease -}}
  {{- printf "app.kubernetes.io/component=triton,app.kubernetes.io/instance=%s" .Values.supersonicRelease -}}
{{- else -}}
  {{- printf "app.kubernetes.io/component=triton" -}}
{{- end -}}
{{- end -}}

{{/*
PromQL label matchers appended to every nv_* query.
*/}}
{{- define "modelManager.prometheusSelector" -}}
{{- if .Values.prometheus.selector -}}
  {{- .Values.prometheus.selector -}}
{{- else if .Values.supersonicRelease -}}
  {{- printf "release=\"%s\"" .Values.supersonicRelease -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret holding basic-auth credentials
*/}}
{{- define "modelManager.authSecretName" -}}
{{- if .Values.auth.existingSecret -}}
  {{- .Values.auth.existingSecret -}}
{{- else -}}
  {{- printf "%s-auth" (include "modelManager.name" .) -}}
{{- end -}}
{{- end -}}

{{/*
Fail early on configurations that cannot work.
*/}}
{{- define "modelManager.validate" -}}
{{- if and .Values.auth.enabled (not .Values.auth.existingSecret) (not .Values.auth.password) -}}
  {{- fail "auth.enabled is true but no credentials were provided. Set auth.password, or auth.existingSecret, or disable auth.enabled." -}}
{{- end -}}
{{- if and .Values.ingress.enabled (not .Values.ingress.hostName) -}}
  {{- fail "ingress.enabled is true but ingress.hostName is empty." -}}
{{- end -}}
{{- end -}}
