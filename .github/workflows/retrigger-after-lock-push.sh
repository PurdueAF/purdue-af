#!/usr/bin/env bash
# After a lockfile push: start CI on the new tip. A GITHUB_TOKEN push leaves any
# pull_request run in action_required, so approve it through the API; with none, dispatch.
#
# Usage: retrigger-after-lock-push.sh <branch>
set -euo pipefail

BRANCH="${1:?branch}"
NEW_SHA="$(git rev-parse HEAD)"
REPO="${GITHUB_REPOSITORY:?}"

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
