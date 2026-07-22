#!/usr/bin/env bash
# After a lockfile push: start CI on the new tip without a human clicking
# "Approve workflows to run".
#
#   AF_RELEASE_TOKEN (PAT) push → pull_request/push fires normally.
#   GITHUB_TOKEN push            → GitHub creates pull_request runs in
#                                  action_required; we approve them via API.
#                                  If none appear, fall back to workflow_dispatch.
#
# Usage: retrigger-after-lock-push.sh <have_pat true|false> <branch>
set -euo pipefail

HAVE_PAT="${1:?have_pat}"
BRANCH="${2:?branch}"
NEW_SHA="$(git rev-parse HEAD)"
REPO="${GITHUB_REPOSITORY:?}"

if [ "$HAVE_PAT" = "true" ]; then
	echo "PAT push re-triggers CI automatically."
	exit 0
fi

approved=0
for _ in $(seq 1 12); do
	ids="$(gh api "repos/${REPO}/actions/runs?event=pull_request&status=action_required&per_page=50" \
		--jq "[.workflow_runs[] | select(.head_sha == \"${NEW_SHA}\")] | .[].id")"
	if [ -n "$ids" ]; then
		while IFS= read -r id; do
			[ -z "$id" ] && continue
			if gh api --method POST "repos/${REPO}/actions/runs/${id}/approve" >/dev/null; then
				echo "Approved workflow run ${id} for ${NEW_SHA}"
				approved=$((approved + 1))
			fi
		done <<<"$ids"
		break
	fi
	sleep 5
done

if [ "$approved" -gt 0 ]; then
	echo "Approved ${approved} awaiting CI run(s) on ${BRANCH}@${NEW_SHA:0:12}"
	exit 0
fi

# No action_required run (e.g. push to main with no open PR): dispatch.
gh workflow run ci.yml --ref "$BRANCH"
echo "Dispatched CI on ${BRANCH} (no action_required run found to approve)"
