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
{{ .Values.ray.image.repository }}:{{ .Values.ray.version }}-{{ .Values.ray.image.flavor }}
{{- end -}}

{{- define "sonic-ray.tritonImage" -}}
{{ .Values.triton.image.repository }}:{{ .Values.triton.image.tag }}
{{- end -}}

{{/*
Where the forwarder's ConfigMap and the pip-installed stubs are mounted; both
are on PYTHONPATH.
*/}}
{{- define "sonic-ray.codeDir" -}}/serve_app{{- end -}}
{{- define "sonic-ray.depsDir" -}}/python-deps{{- end -}}

{{/*
Hash of the forwarder's source, annotated onto both pod templates so a code
change rolls the cluster like any other change to it would.
*/}}
{{- define "sonic-ray.codeChecksum" -}}
{{ (.Files.Glob "files/sonic_ray/*.py").AsConfig | sha256sum }}
{{- end -}}

{{/*
Shared by head and worker Ray containers: environment, the code + deps
mounts, and the init container that pip-installs Triton's stubs.
*/}}
{{- define "sonic-ray.rayEnv" -}}
- name: PYTHONPATH
  value: {{ printf "%s:%s" (include "sonic-ray.codeDir" .) (include "sonic-ray.depsDir" .) | quote }}
{{- end -}}

{{- define "sonic-ray.rayMounts" -}}
- { name: log-volume, mountPath: /tmp/ray }
- name: code
  mountPath: {{ include "sonic-ray.codeDir" . }}/sonic_ray
  readOnly: true
- name: python-deps
  mountPath: {{ include "sonic-ray.depsDir" . }}
  readOnly: true
{{- end -}}

{{- define "sonic-ray.rayVolumes" -}}
- name: log-volume
  emptyDir: {}
- name: code
  configMap:
    name: {{ include "sonic-ray.name" . }}-code
- name: python-deps
  emptyDir: {}
{{- end -}}

{{- define "sonic-ray.pipInitContainer" -}}
# Triton's generated gRPC servicer must be importable by Serve's proxies,
# which run outside any runtime_env — so it goes on PYTHONPATH for the
# whole pod. --no-deps keeps the image's grpcio/protobuf/numpy in charge.
- name: pip-install
  image: {{ include "sonic-ray.rayImage" . }}
  imagePullPolicy: {{ .Values.ray.image.pullPolicy }}
  command: ["pip", "install", "--no-cache-dir", "--no-deps", "--target", {{ include "sonic-ray.depsDir" . | quote }}]
  args:
    {{- toYaml .Values.python.pip | nindent 4 }}
  volumeMounts:
    - name: python-deps
      mountPath: {{ include "sonic-ray.depsDir" . }}
  resources:
    limits: { cpu: "1", memory: 1Gi }
    requests: { cpu: 100m, memory: 256Mi }
{{- end -}}

{{/*
Refuse to render what cannot work.
*/}}
{{- define "sonic-ray.validate" -}}
{{- if not (.Files.Glob "files/sonic_ray/*.py") -}}
  {{- fail "files/sonic_ray/*.py is empty: nothing to serve." -}}
{{- end -}}
{{- if not (regexMatch "(^| )tritonclient==" (join " " .Values.python.pip)) -}}
  {{- fail "python.pip must pin tritonclient==<version>: it carries Triton's gRPC servicer for Serve's proxy." -}}
{{- end -}}
{{- if not .Values.triton.modelRepository.claimName -}}
  {{- fail "triton.modelRepository.claimName is required: the PVC holding the Triton model repository." -}}
{{- end -}}
{{- if gt (int .Values.serve.minReplicas) (int .Values.serve.maxReplicas) -}}
  {{- fail "serve.minReplicas exceeds serve.maxReplicas." -}}
{{- end -}}
{{- if gt (int .Values.ray.worker.minReplicas) (int .Values.ray.worker.maxReplicas) -}}
  {{- fail "ray.worker.minReplicas exceeds ray.worker.maxReplicas." -}}
{{- end -}}
{{- if gt (int .Values.serve.maxReplicas) (int .Values.ray.worker.maxReplicas) -}}
  {{- fail (printf "serve.maxReplicas (%d) exceeds ray.worker.maxReplicas (%d): each replica fronts its own Triton pod, so the extra replicas could never be placed." (int .Values.serve.maxReplicas) (int .Values.ray.worker.maxReplicas)) -}}
{{- end -}}
{{- $gpus := index .Values.triton.resources.limits "nvidia.com/gpu" | default 0 | int -}}
{{- if ne $gpus 1 -}}
  {{- fail "triton.resources.limits must request exactly one nvidia.com/gpu: a pod is one Triton on one GPU." -}}
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
{{- if le (int .Values.ray.worker.terminationGracePeriodSeconds) (int .Values.serve.gracefulShutdownTimeoutS) -}}
  {{- fail (printf "ray.worker.terminationGracePeriodSeconds (%d) must exceed serve.gracefulShutdownTimeoutS (%d)." (int .Values.ray.worker.terminationGracePeriodSeconds) (int .Values.serve.gracefulShutdownTimeoutS)) -}}
{{- end -}}
{{- end -}}
