#!/usr/bin/env bash
#
# Validate everything Flux consumes, the way Flux consumes it: render each root,
# kubeconform the result, `helm template` every HelmRelease with its resolved
# values, and `promtool check and test` the rules in Helm values.
#
# Requirements: kustomize, flux, yq (v4), kubeconform, helm, promtool
# Run locally:  ./.github/workflows/validate-manifests.sh
#
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# The cluster's Kubernetes version, which chart kubeVersion constraints validate against.
KUBE_VERSION="${KUBE_VERSION:-1.29.0}"

# shellcheck disable=SC2054  # commas below are kubeconform list syntax
KUBECONFORM=(
	kubeconform
	-summary
	-strict
	-kubernetes-version "$KUBE_VERSION"
	-schema-location default
	-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
)

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
failed=0
releases_seen=0

# Render a root like kustomize-controller, except that an undeclared variable fails here.
# The caller's `if` turns errexit off, so each step exits by hand.
SUBST_DISABLED='.metadata.annotations["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"'
render_env() {
	local env_dir=$1
	(
		while IFS='=' read -r key value; do
			export "${key}=${value}"
		done < <(yq -r '.spec.postBuild.substitute // {} | to_entries[] | .key + "=" + .value' \
			"${env_dir}flux-kustomization.yaml")
		kustomize build --load-restrictor LoadRestrictionsNone "$env_dir" >"$workdir/raw.yaml" || exit 1
		yq "select(${SUBST_DISABLED} | not)" "$workdir/raw.yaml" | flux envsubst --strict || exit 1
		echo "---"
		yq "select(${SUBST_DISABLED})" "$workdir/raw.yaml"
	)
}

# A network call fails for reasons unrelated to the manifests; a broken chart fails every attempt.
HELM_ATTEMPTS="${HELM_ATTEMPTS:-3}"
HELM_RETRY_DELAY="${HELM_RETRY_DELAY:-5}"
retry() {
	local label=$1
	shift
	local attempt=1 out
	while :; do
		if out=$("$@" 2>&1); then
			return 0
		fi
		if ((attempt >= HELM_ATTEMPTS)); then
			printf '%s\n' "$out" >&2
			return 1
		fi
		echo "  ${label}: attempt ${attempt} failed, retrying in ${HELM_RETRY_DELAY}s" >&2
		attempt=$((attempt + 1))
		sleep "$HELM_RETRY_DELAY"
	done
}

helm_template_retry() {
	local name=$1 version=$2
	shift 2
	retry "${name}: helm template" helm template "$name" "$@" \
		${version:+--version "$version"} \
		--kube-version "$KUBE_VERSION" \
		--namespace cms
}

# A branch, a tag, a commit or HEAD; the destination exists only once the checkout succeeded.
fetch_ref() {
	local url=$1 ref=$2 dest=$3 tmp
	tmp=$(mktemp -d "$workdir/fetch.XXXXXX")
	if git -C "$tmp" init --quiet &&
		retry "${url}@${ref}: git fetch" env GIT_TERMINAL_PROMPT=0 git -C "$tmp" fetch --quiet --depth 1 "$url" "$ref" &&
		git -C "$tmp" checkout --quiet FETCH_HEAD; then
		mv "$tmp" "$dest"
	else
		rm -rf "$tmp"
		return 1
	fi
}

# Fetch a GitRepository source once per (url, ref) and echo the chart's path.
# `dependency update`, not `build`: build wants every repository already added to helm.
git_chart_dir() {
	local url=$1 ref=$2 chart_path=$3
	local key clone_dir
	key=$(printf '%s@%s' "$url" "$ref" | shasum | cut -d' ' -f1)
	clone_dir="$workdir/gitsrc-$key"
	if [[ ! -d "$clone_dir" ]]; then
		fetch_ref "$url" "$ref" "$clone_dir" || return 1
	fi
	[[ -d "$clone_dir/$chart_path" ]] || {
		echo "chart path '${chart_path}' not in ${url}@${ref}" >&2
		return 1
	}
	# An apiVersion v1 chart lists its dependencies in requirements.yaml.
	if [[ -f "$clone_dir/$chart_path/Chart.yaml" ]] &&
		[[ ! -d "$clone_dir/$chart_path/charts" ]] &&
		{ [[ $(yq -N '.dependencies | length' "$clone_dir/$chart_path/Chart.yaml") -gt 0 ]] ||
			[[ -f "$clone_dir/$chart_path/requirements.yaml" ]]; }; then
		if ! retry "${chart_path}: helm dependency update" helm dependency update "$clone_dir/$chart_path"; then
			echo "could not fetch chart dependencies for ${chart_path} in ${url}@${ref}" >&2
			return 1
		fi
	fi
	printf '%s' "$clone_dir/$chart_path"
}

# Identical (repo, chart, version, values) combinations render once.
# A newline-joined list, not an assoc array: macOS ships bash 3.2.
seen_releases=""
validate_helmreleases() {
	local rendered=$1
	local repos_file=$2     # "name|type|url" lines from every root
	local git_repos_file=$3 # "name|url|ref" lines from every root
	local name release chart version src_name src_kind repo_line repo_url repo_type
	local values_files values_file values_args vhash kind src key path i local_chart git_ref git_dir
	while IFS= read -r name; do
		releases_seen=$((releases_seen + 1))
		release="select(.kind==\"HelmRelease\" and .metadata.name==\"$name\")"
		chart=$(yq "$release | .spec.chart.spec.chart" "$rendered")
		version=$(yq "$release | .spec.chart.spec.version" "$rendered")
		src_name=$(yq "$release | .spec.chart.spec.sourceRef.name" "$rendered")
		src_kind=$(yq "$release | .spec.chart.spec.sourceRef.kind" "$rendered")

		local_chart=""
		git_ref=""
		git_dir=""
		case "$src_kind" in
		HelmRepository)
			if [[ -z "$version" || "$version" == "null" ]]; then
				echo "✗ ${name}: chart version is not pinned (omitted version = Flux silently tracks latest)" >&2
				failed=1
				continue
			fi
			repo_line=$(grep -m1 "^${src_name}|" "$repos_file" || true)
			if [[ -z "$repo_line" ]]; then
				echo "✗ ${name}: HelmRepository '${src_name}' not found in any rendered environment" >&2
				failed=1
				continue
			fi
			repo_type=$(cut -d'|' -f2 <<<"$repo_line")
			repo_url=$(cut -d'|' -f3- <<<"$repo_line")
			;;
		GitRepository)
			if [[ "$chart" == ./* || "$chart" == /* ]]; then
				# A relative path is this repository's own chart, rendered from the working tree.
				if [[ ! -f "$chart/Chart.yaml" ]]; then
					echo "✗ ${name}: chart path '${chart}' has no Chart.yaml in this repository" >&2
					failed=1
					continue
				fi
				local_chart="$chart"
			else
				repo_line=$(grep -m1 "^${src_name}|" "$git_repos_file" || true)
				if [[ -z "$repo_line" ]]; then
					echo "✗ ${name}: GitRepository '${src_name}' not found in any rendered environment" >&2
					failed=1
					continue
				fi
				repo_type="git"
				repo_url=$(cut -d'|' -f2 <<<"$repo_line")
				git_ref=$(cut -d'|' -f3- <<<"$repo_line")
				# A git-sourced chart has no registry version; the ref is its identity.
				version=""
			fi
			;;
		*)
			echo "  skip ${name}: chart sourced from ${src_kind} '${src_name}'"
			continue
			;;
		esac

		values_files=()
		i=0
		while IFS='|' read -r kind src key path; do
			[[ -z "$src" ]] && continue
			values_file="$workdir/${name}-values-${i}.yaml"
			: >"$values_file"
			if [[ "$kind" == "Secret" && -n "$path" ]]; then
				if yq "select(.kind==\"Secret\" and .metadata.name==\"$src\") | (.data // .stringData) | has(\"$key\")" \
					"$rendered" | grep -qx true; then
					yq -n ".${path} = \"placeholder\"" >"$values_file"
				fi
			else
				yq "select(.kind==\"ConfigMap\" and .metadata.name==\"$src\") | .data[\"$key\"]" \
					"$rendered" >"$values_file"
			fi
			if [[ ! -s "$values_file" ]]; then
				echo "✗ ${name}: valuesFrom ${kind} '${src}' (key ${key}) not found" >&2
				failed=1
				continue 2
			fi
			values_files+=("$values_file")
			i=$((i + 1))
		done < <(yq "$release | .spec.valuesFrom[]? | (.kind // \"ConfigMap\") + \"|\" + .name + \"|\" + (.valuesKey // \"values.yaml\") + \"|\" + (.targetPath // \"\")" "$rendered")

		if [[ $(yq "$release | .spec | has(\"values\")" "$rendered") == "true" ]]; then
			yq "$release | .spec.values" "$rendered" >"$workdir/${name}-values-inline.yaml"
			values_files+=("$workdir/${name}-values-inline.yaml")
		fi

		# The chart hard-fails when `lookup` finds no ConfigMap, and `helm template` has no cluster.
		if [[ ${#values_files[@]} -gt 0 ]] &&
			yq -N '(.envoy.external_config.load_from_configmap // false)' \
				"${values_files[@]}" | grep -qx true; then
			echo "  skip ${name}: Envoy config is looked up from a ConfigMap at render time"
			continue
		fi

		values_args=()
		vhash="none"
		if [[ ${#values_files[@]} -gt 0 ]]; then
			vhash=$(cat "${values_files[@]}" | shasum | cut -d' ' -f1)
			for f in "${values_files[@]}"; do
				values_args+=(-f "$f")
			done
		fi

		if [[ -n "$local_chart" ]]; then
			echo "  helm template ${name} (${local_chart}, from this repository)"
			if ! helm_template_retry "$name" "" "$local_chart" \
				${values_args[@]+"${values_args[@]}"}; then
				echo "✗ ${name}: helm template failed" >&2
				failed=1
			fi
			continue
		fi

		local fingerprint="${repo_url}|${git_ref}|${chart}|${version}|${vhash}"
		if [[ "$seen_releases" == *"$fingerprint"* ]]; then
			continue
		fi
		seen_releases="${seen_releases}${fingerprint}"$'\n'

		echo "  helm template ${name} (${chart}@${version:-${git_ref}} from ${repo_url})"
		local helm_src
		if [[ "$repo_type" == "git" ]]; then
			if ! git_dir=$(git_chart_dir "$repo_url" "$git_ref" "$chart"); then
				echo "✗ ${name}: could not fetch chart from ${repo_url}@${git_ref}" >&2
				failed=1
				continue
			fi
			helm_src=("$git_dir")
		elif [[ "$repo_type" == "oci" ]]; then
			helm_src=("${repo_url}/${chart}")
		else
			helm_src=("$chart" --repo "$repo_url")
		fi
		if ! helm_template_retry "$name" "$version" "${helm_src[@]}" \
			${values_args[@]+"${values_args[@]}"}; then
			echo "✗ ${name}: helm template failed" >&2
			failed=1
		fi
	done < <(yq -N 'select(.kind=="HelmRelease") | .metadata.name' "$rendered")
}

# --- Flux-managed environments -------------------------------------------
for env_dir in flux/*/; do
	[[ -f "${env_dir}kustomization.yaml" ]] || continue
	name=$(basename "$env_dir")
	rendered="$workdir/rendered-${name}.yaml"
	if ! render_env "$env_dir" >"$rendered"; then
		echo "✗ ${name}: render failed" >&2
		failed=1
	fi
done
# Named outside the rendered-*.yaml glob: the redirect creates it before the glob expands.
for f in "$workdir"/rendered-*.yaml; do
	cat "$f"
	echo "---"
done >"$workdir/union.yaml"

# A release may reference a source created by a sibling Flux Kustomization.
yq -N 'select(.kind=="HelmRepository") | .metadata.name + "|" + (.spec.type // "default") + "|" + .spec.url' \
	"$workdir/union.yaml" | sort -u >"$workdir/helm-repos.txt"

yq -N 'select(.kind=="GitRepository") | .metadata.name + "|" + .spec.url + "|" + (.spec.ref.commit // .spec.ref.tag // .spec.ref.branch // "HEAD")' \
	"$workdir/union.yaml" | sort -u >"$workdir/git-repos.txt"

for rendered in "$workdir"/rendered-*.yaml; do
	name=$(basename "$rendered" .yaml)
	echo "──── ${name#rendered-} ────"
	# Flux decrypts a SOPS Secret before applying it; its `sops` block never reaches the API server.
	yq 'del(.sops)' "$rendered" | "${KUBECONFORM[@]}" || failed=1
	validate_helmreleases "$rendered" "$workdir/helm-repos.txt" "$workdir/git-repos.txt"
done

# --- Flux bootstrap objects ----------------------------------------------
echo "──── flux bootstrap objects ────"
if ! "${KUBECONFORM[@]}" flux/*/flux-kustomization.yaml flux/*/git-repository.yaml; then
	failed=1
fi

# --- Prometheus rules ------------------------------------------------------
echo "──── prometheus rules ────"
for values_file in $(git grep -lE "alerting_rules|recording_rules" -- 'apps/**/values.yaml'); do
	for key in alerting_rules.yml recording_rules.yml; do
		[[ $(yq ".serverFiles | has(\"$key\")" "$values_file") == "true" ]] || continue
		yq ".serverFiles[\"$key\"]" "$values_file" >"$workdir/rules.yml"
		echo "  promtool check ${values_file} → ${key}"
		if ! promtool check rules "$workdir/rules.yml" >/dev/null; then
			echo "✗ ${values_file}: ${key} failed promtool validation" >&2
			promtool check rules "$workdir/rules.yml" || true
			failed=1
		fi
	done
done
yq '.serverFiles["alerting_rules.yml"]' apps/monitoring/prometheus/values.yaml >"$workdir/rules.yml"
yq '.serverFiles["recording_rules.yml"]' apps/monitoring/prometheus/values.yaml >"$workdir/recording.yml"
cp tests/manifests/alerts.test.yml "$workdir/"
echo "  promtool test tests/manifests/alerts.test.yml"
if ! promtool test rules "$workdir/alerts.test.yml"; then
	echo "✗ tests/manifests/alerts.test.yml failed against the alert rules" >&2
	failed=1
fi

# --- Tripwires against silent pass-through --------------------------------
# A count this low means the extraction broke, not that the platform shrank.
total_resources=$(yq -N 'select(.kind != null) | .kind' "$workdir/union.yaml" | grep -c .)
if [[ $releases_seen -lt 10 || $total_resources -lt 100 ]]; then
	echo "✗ tripwire: only ${releases_seen} HelmReleases / ${total_resources} resources found — validator is broken" >&2
	failed=1
fi
echo "(validated ${releases_seen} HelmRelease references, ${total_resources} resources)"

if [[ $failed -ne 0 ]]; then
	echo "✗ manifest validation FAILED" >&2
	exit 1
fi
echo "✓ all manifests valid"
