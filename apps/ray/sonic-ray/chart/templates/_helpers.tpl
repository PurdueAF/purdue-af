{{/*
Instance name (equal to release name unless overridden)
*/}}
{{- define "sonic-ray.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sonic-ray.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ include "sonic-ray.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
What the Services select pods by. Deliberately not app.kubernetes.io/name:
KubeRay stamps its own value for that key onto every pod it creates, and
whether a template's value survives is a detail of the operator version.
app.kubernetes.io/instance and /component are ours alone.
*/}}
{{- define "sonic-ray.podSelector" -}}
app.kubernetes.io/instance: {{ include "sonic-ray.name" . }}
{{- end -}}

{{- define "sonic-ray.rayImage" -}}
{{ .Values.ray.image.repository }}:{{ .Values.ray.image.tag | default .Values.ray.version }}
{{- end -}}

{{- define "sonic-ray.tritonImage" -}}
{{ .Values.triton.image.repository }}:{{ .Values.triton.image.tag }}
{{- end -}}

{{/*
Refuse to render what cannot work.
*/}}
{{- define "sonic-ray.validate" -}}
{{- if not .Values.triton.modelRepository.claimName -}}
  {{- fail "triton.modelRepository.claimName is required: the PVC holding the Triton model repository." -}}
{{- end -}}
{{- if gt (int .Values.autoscaling.min_servers) (int .Values.autoscaling.max_servers) -}}
  {{- fail "autoscaling.min_servers exceeds autoscaling.max_servers." -}}
{{- end -}}
{{- $args := join " " .Values.triton.args -}}
{{- if not (contains .Values.triton.modelRepository.mountPath $args) -}}
  {{- fail (printf "triton.args never mention triton.modelRepository.mountPath (%s): Triton would not see the repository that is mounted." .Values.triton.modelRepository.mountPath) -}}
{{- end -}}
{{- with regexFind "--exit-timeout-secs=[0-9]+" $args -}}
  {{- $exit := trimPrefix "--exit-timeout-secs=" . | int -}}
  {{- if le (int $.Values.ray.worker.terminationGracePeriodSeconds) $exit -}}
    {{- fail (printf "ray.worker.terminationGracePeriodSeconds (%d) must exceed Triton's --exit-timeout-secs (%d), or a scale-down kills in-flight requests." (int $.Values.ray.worker.terminationGracePeriodSeconds) $exit) -}}
  {{- end -}}
{{- end -}}
{{- end -}}
