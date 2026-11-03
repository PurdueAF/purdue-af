#!/bin/bash
# Sync an AF userlist Secret from an upstream user registry.
#
#   sync-userlist.sh cern    — CMS members via CERN CRIC (x509-authenticated)
#   sync-userlist.sh purdue  — Purdue accounts via Hammer LDAP
#
# A failed fetch, or an empty, short or malformed list, fails the job and leaves the
# existing Secret untouched (AFUserlistNotSyncing).
set -euo pipefail

SOURCE=${1:?usage: sync-userlist.sh cern|purdue}
SECRET_NAME="af-auth-${SOURCE}"
MIN_USERS=${MIN_USERS:-200}
# Share of the current list a new one must keep: a list that shrank more is suspect.
MIN_KEPT_PERCENT=${MIN_KEPT_PERCENT:-80}
KUBECTL_VERSION=v1.30.14
# Retry in-pod: only transient faults are helped; a pod-bound fault repeats every attempt.
FETCH_ATTEMPTS=${FETCH_ATTEMPTS:-5}
FETCH_RETRY_DELAY=${FETCH_RETRY_DELAY:-60}
TMP_FILE=$(mktemp)

ensure_tools() {
	# Installer output MUST go to stderr: this runs inside the captured fetch functions.
	{
		local missing=()
		for tool in "$@"; do
			command -v "$tool" >/dev/null || missing+=("$tool")
		done
		if [ ${#missing[@]} -gt 0 ]; then
			dnf install -y "${missing[@]/#ldapsearch/openldap-clients}"
		fi
		if ! command -v kubectl >/dev/null; then
			local url="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
			curl -fsSL -o /tmp/kubectl "$url"
			echo "$(curl -fsSL "${url}.sha256")  /tmp/kubectl" | sha256sum --check --quiet
			install -m 0755 /tmp/kubectl /usr/local/bin/kubectl
		fi
	} 1>&2
}

# Pipelines end in `|| true`: pipefail would skip the retries, and the gates below catch bad data.
fetch_cern() {
	ensure_tools curl jq
	curl --cacert /scripts/CERN-bundle.pem \
		--cert /etc/grid-security-ro/x509up \
		--key /etc/grid-security-ro/x509up \
		"https://cms-cric.cern.ch/api/accounts/user/query/?json" |
		jq -r '.[] | .profiles[] | .dn | select(. != null)' |
		grep "/OU=Users/" |
		sed -n 's/.*\/OU=Users\/CN=\([^\/]*\)\/CN=.*/\1/p' |
		sort -u || true
}

fetch_purdue() {
	ensure_tools ldapsearch
	local entries rc=0
	entries=$(ldapsearch -H ldap://auth.hammer.rcac.purdue.edu -x \
		-b "ou=People,dc=hammer,dc=rcac,dc=purdue,dc=edu" "(uid=*)" uid) || rc=$?
	# A size or time limit prints entries too, and a cut list must not pass the gates.
	if [ "$rc" -ne 0 ]; then
		echo "ldapsearch exited ${rc}: discarding its result" >&2
		return 0
	fi
	grep '^uid:' <<<"$entries" | cut -d ' ' -f2 | sort -u || true
}

# Retry only while the result is unusable; the validation gates give the verdict.
fetch_with_retry() {
	local attempt count
	for attempt in $(seq 1 "$FETCH_ATTEMPTS"); do
		"fetch_${SOURCE}" >"$TMP_FILE"
		count=$(grep -c . "$TMP_FILE" || true)
		if [ "$count" -ge "$MIN_USERS" ]; then
			return 0
		fi
		if [ "$attempt" -lt "$FETCH_ATTEMPTS" ]; then
			echo "attempt ${attempt}/${FETCH_ATTEMPTS}: got ${count} users" \
				"(< ${MIN_USERS}) — retrying in ${FETCH_RETRY_DELAY}s"
			sleep "$FETCH_RETRY_DELAY"
		fi
	done
}

echo "Fetching ${SOURCE} users..."
fetch_with_retry

# --- Validate before touching the Secret -----------------------------------
if [ ! -s "$TMP_FILE" ]; then
	echo "ERROR: ${SOURCE} user list is empty — refusing to update ${SECRET_NAME}"
	exit 1
fi
USER_COUNT=$(grep -c . "$TMP_FILE")
echo "Found ${USER_COUNT} users"
if [ "$USER_COUNT" -lt "$MIN_USERS" ]; then
	echo "ERROR: only ${USER_COUNT} users found (< ${MIN_USERS}) — refusing to update ${SECRET_NAME}"
	exit 1
fi
if grep -q " " "$TMP_FILE" || grep -q "^$" "$TMP_FILE"; then
	echo "ERROR: invalid data format in user list — refusing to update ${SECRET_NAME}"
	exit 1
fi

# --- Upsert the Secret, reporting the diff ----------------------------------
if kubectl get secret "$SECRET_NAME" >/dev/null 2>&1; then
	kubectl get secret "$SECRET_NAME" -o jsonpath='{.data.userlist}' |
		base64 -d >/tmp/existing-users.txt
	EXISTING_COUNT=$(grep -c . /tmp/existing-users.txt || true)
	if [ $((USER_COUNT * 100)) -lt $((EXISTING_COUNT * MIN_KEPT_PERCENT)) ]; then
		echo "ERROR: ${USER_COUNT} users is under ${MIN_KEPT_PERCENT}% of the current ${EXISTING_COUNT}" \
			"— refusing to update ${SECRET_NAME}"
		exit 1
	fi

	echo "=== User List Changes ==="
	comm -13 <(sort /tmp/existing-users.txt) <(sort "$TMP_FILE") >/tmp/added-users.txt
	if [ -s /tmp/added-users.txt ]; then
		echo "Added users:" && cat /tmp/added-users.txt
	else
		echo "No new users added"
	fi
	comm -23 <(sort /tmp/existing-users.txt) <(sort "$TMP_FILE") >/tmp/removed-users.txt
	if [ -s /tmp/removed-users.txt ]; then
		echo "Removed users:" && cat /tmp/removed-users.txt
	else
		echo "No users removed"
	fi
	echo "========================"

	kubectl patch secret "$SECRET_NAME" --type=merge \
		-p="{\"data\":{\"userlist\":\"$(base64 -w0 "$TMP_FILE")\"}}"
else
	echo "Secret does not exist, creating it with ${USER_COUNT} users..."
	kubectl create secret generic "$SECRET_NAME" --from-file=userlist="$TMP_FILE"
fi

echo "${SECRET_NAME} sync complete"
