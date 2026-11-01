# shellcheck shell=bash
# Sourced. ghcr_retry <what> <command...> retries a transiently failing GHCR call. Progress goes to stderr: callers capture stdout.
ghcr_retry() {
	local what="$1"
	shift
	local attempt=1 max=5 delay="${GHCR_RETRY_DELAY:-15}"
	until "$@"; do
		if [ "$attempt" -ge "$max" ]; then
			echo "::error::${what} failed after ${max} attempts" >&2
			return 1
		fi
		echo "::warning::${what} failed (attempt ${attempt}/${max}); retrying in ${delay}s" >&2
		sleep "$delay"
		attempt=$((attempt + 1))
		delay=$((delay * 2))
	done
}
