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

{{- define "sonic-ray.image" -}}
{{ .Values.ray.image.repository }}:{{ .Values.ray.version }}-{{ .Values.ray.image.flavor }}
{{- end -}}

{{/*
Where the ConfigMap with the server code is mounted; its parent is PYTHONPATH.
*/}}
{{- define "sonic-ray.codeDir" -}}/serve_app{{- end -}}

{{/*
Hash of the server code, annotated onto both pod templates so a code change
rolls the cluster like any other change to it would.
*/}}
{{- define "sonic-ray.codeChecksum" -}}
{{ (.Files.Glob "files/sonic_ray/*.py").AsConfig | sha256sum }}
{{- end -}}

{{/*
Environment the sonic_ray package reads, identical on head and workers so an
import on either sees the same configuration.
*/}}
{{- define "sonic-ray.env" -}}
- name: PYTHONPATH
  value: {{ include "sonic-ray.codeDir" . | quote }}
- name: MODEL_REPOSITORY
  value: {{ .Values.modelRepository.mountPath | quote }}
- name: ONNX_EXECUTION_PROVIDERS
  value: {{ .Values.executionProviders | quote }}
- name: LOG_LEVEL
  value: {{ .Values.logLevel | quote }}
{{- end -}}

{{/*
Refuse to render what cannot work.
*/}}
{{- define "sonic-ray.validate" -}}
{{- if not (.Files.Glob "files/sonic_ray/*.py") -}}
  {{- fail "files/sonic_ray/*.py is empty: nothing to serve." -}}
{{- end -}}
{{- if not (regexMatch "^onnxruntime-gpu==" (join " " .Values.serve.pip)) -}}
  {{- fail "serve.pip must pin onnxruntime-gpu==<version>: the stock Ray image has no inference runtime." -}}
{{- end -}}
{{- if not .Values.modelRepository.claimName -}}
  {{- fail "modelRepository.claimName is required: the PVC holding the model repository." -}}
{{- end -}}
{{- if gt (int .Values.serve.minReplicas) (int .Values.serve.maxReplicas) -}}
  {{- fail "serve.minReplicas exceeds serve.maxReplicas." -}}
{{- end -}}
{{- if gt (int .Values.ray.worker.minReplicas) (int .Values.ray.worker.maxReplicas) -}}
  {{- fail "ray.worker.minReplicas exceeds ray.worker.maxReplicas." -}}
{{- end -}}
{{- $gpus := index .Values.ray.worker.resources.limits "nvidia.com/gpu" | default 0 | int -}}
{{- if ne $gpus 1 -}}
  {{- fail "ray.worker.resources.limits must request exactly one nvidia.com/gpu: a replica is one GPU, a pod is one replica." -}}
{{- end -}}
{{- if gt (int .Values.serve.maxReplicas) (int .Values.ray.worker.maxReplicas) -}}
  {{- fail (printf "serve.maxReplicas (%d) exceeds ray.worker.maxReplicas (%d): each replica needs its own GPU pod, so the extra replicas could never be placed." (int .Values.serve.maxReplicas) (int .Values.ray.worker.maxReplicas)) -}}
{{- end -}}
{{- if le (int .Values.ray.worker.terminationGracePeriodSeconds) (int .Values.serve.gracefulShutdownTimeoutS) -}}
  {{- fail (printf "ray.worker.terminationGracePeriodSeconds (%d) must exceed serve.gracefulShutdownTimeoutS (%d), or a scale-down kills in-flight requests." (int .Values.ray.worker.terminationGracePeriodSeconds) (int .Values.serve.gracefulShutdownTimeoutS)) -}}
{{- end -}}
{{- end -}}
