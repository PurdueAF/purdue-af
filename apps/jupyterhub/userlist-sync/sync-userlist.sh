#!/bin/bash
# Sync an AF userlist Secret from an upstream user registry.
#
#   sync-userlist.sh cern    — CMS members via CERN CRIC (x509-authenticated)
#   sync-userlist.sh purdue  — Purdue accounts via Hammer LDAP
#
# The resulting Secret (af-auth-<source>) is what the JupyterHub spawner's
# auth gate reads, so this script refuses to write anything that looks wrong:
# empty lists, suspiciously small lists, or malformed usernames all fail the
# job and leave the existing Secret untouched.
set -euo pipefail

SOURCE=${1:?usage: sync-userlist.sh cern|purdue}
SECRET_NAME="af-auth-${SOURCE}"
MIN_USERS=${MIN_USERS:-200}
TMP_FILE=$(mktemp)

ensure_tools() {
	# Install only what is missing — a no-op in tests and prebaked images.
	# Runs inside the captured fetch functions, so every line of installer
	# output MUST go to stderr or it would end up in the userlist.
	{
		local missing=()
		for tool in "$@"; do
			command -v "$tool" >/dev/null || missing+=("$tool")
		done
		if [ ${#missing[@]} -gt 0 ]; then
			dnf install -y "${missing[@]/#ldapsearch/openldap-clients}" --nogpgcheck
		fi
		if ! command -v kubectl >/dev/null; then
			curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
			chmod +x kubectl
			mv kubectl /usr/local/bin/
		fi
	} 1>&2
}

fetch_cern() {
	ensure_tools curl jq
	curl -k \
		--cert /etc/grid-security-ro/x509up \
		--key /etc/grid-security-ro/x509up \
		"https://cms-cric.cern.ch/api/accounts/user/query/?json" |
		jq -r '.[] | .profiles[] | .dn | select(. != null)' |
		grep "/OU=Users/" |
		sed -n 's/.*\/OU=Users\/CN=\([^\/]*\)\/CN=.*/\1/p' |
		sort -u
}

fetch_purdue() {
	ensure_tools ldapsearch
	ldapsearch -H ldap://auth.hammer.rcac.purdue.edu -x \
		-b "ou=People,dc=hammer,dc=rcac,dc=purdue,dc=edu" uid |
		grep uid: | cut -d " " -f2
}

echo "Fetching ${SOURCE} users..."
"fetch_${SOURCE}" >"$TMP_FILE"

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
