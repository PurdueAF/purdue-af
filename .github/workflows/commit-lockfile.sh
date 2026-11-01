#!/usr/bin/env bash
# Commit a regenerated pixi lock to the branch, start CI on it, then fail this
# run so nothing is built on the pre-lock tree. Lock commits: RELEASING.md.
#
# Usage: BRANCH=<branch> HEAD_REPO=<PR head repo, empty on push> commit-lockfile.sh <base|global>
set -euo pipefail

ENV_NAME="${1:?base or global}"
LOCK="pixi/${ENV_NAME}/pixi.lock"

if [ "${HEAD_REPO:-$GITHUB_REPOSITORY}" != "$GITHUB_REPOSITORY" ]; then
	echo "::error::${LOCK} is out of sync with pixi.toml." \
		"Fork PRs cannot receive lock commits: run \`pixi lock --manifest-path pixi/${ENV_NAME}/pixi.toml\` and push."
	exit 1
fi

# Renovate's gitIgnoredAuthors names this author, so its branches stay rebasable.
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add "$LOCK"
if git diff --staged --quiet; then
	echo "Lock regenerate produced no file change: nothing to commit"
	exit 0
fi
git commit -m "pixi-${ENV_NAME}: regenerate the lockfile"
git pull --rebase origin "$BRANCH"
git push origin "HEAD:$BRANCH"
.github/workflows/retrigger-after-lock-push.sh "$BRANCH" | tee -a "$GITHUB_STEP_SUMMARY"
exit 1
