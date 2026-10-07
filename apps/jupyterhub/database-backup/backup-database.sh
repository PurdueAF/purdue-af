#!/bin/sh
# Back up the hub sqlite DB to depot storage, keeping the 5 most recent.
set -eu

BACKUP_DIR=/depot/cms/purdue-af/backups
DB_PATH=/srv/jupyterhub/jupyterhub.sqlite

echo "Starting database backup at $(date)"

echo "Searching for JupyterHub hub pods..."
kubectl get pods -n cms -l app=jupyterhub,component=hub

HUB_POD=$(kubectl get pods -n cms -l app=jupyterhub,component=hub \
	-o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$HUB_POD" ]; then
	echo "ERROR: Could not find JupyterHub hub pod"
	kubectl get pods -n cms | grep -i hub || echo "No hub pods found"
	exit 1
fi
echo "Found hub pod: $HUB_POD"

if [ "$(date +%H)" -lt 12 ]; then TIMESTAMP="midnight"; else TIMESTAMP="noon"; fi
BACKUP_FILENAME="jupyterhub-$(date +%Y)$(date +%b | tr '[:upper:]' '[:lower:]')$(date +%d)-${TIMESTAMP}.sqlite"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"
echo "Backup path: ${BACKUP_PATH}"

mkdir -p "$BACKUP_DIR"

echo "Checking if database file exists in pod..."
if ! kubectl exec -n cms "$HUB_POD" -- test -f "$DB_PATH"; then
	echo "ERROR: ${DB_PATH} does not exist in pod ${HUB_POD}"
	kubectl exec -n cms "$HUB_POD" -- find /srv -name "*.sqlite" -o -name "jupyterhub.db" 2>/dev/null ||
		echo "No database files found"
	exit 1
fi

# SQLite's online backup: a file copy of a database the hub is writing can be torn.
echo "Backing up database from pod ${HUB_POD} to ${BACKUP_PATH}..."
if ! kubectl exec -n cms "$HUB_POD" -- python3 -c '
import os, sqlite3, sys, tempfile

src = sqlite3.connect(sys.argv[1], timeout=60)
fd, tmp = tempfile.mkstemp(suffix=".sqlite")
os.close(fd)
try:
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    dst.close()
    with open(tmp, "rb") as f:
        sys.stdout.buffer.write(f.read())
finally:
    os.unlink(tmp)
' "$DB_PATH" >"$BACKUP_PATH"; then
	echo "ERROR: Failed to back up database from pod"
	rm -f "$BACKUP_PATH"
	exit 1
fi

if [ -s "$BACKUP_PATH" ]; then
	echo "Database backup completed successfully: ${BACKUP_PATH}"
	echo "Cleaning up old backups (keeping last 5)..."
	cd "$BACKUP_DIR"
	ls -t jupyterhub-*.sqlite | tail -n +6 | xargs -r rm -f
	echo "Current backups:"
	ls -la jupyterhub-*.sqlite 2>/dev/null || echo "No backups found"
else
	echo "WARNING: Database file is empty or does not exist, removing backup file"
	rm -f "$BACKUP_PATH"
	exit 1
fi

echo "Database backup job completed at $(date)"
