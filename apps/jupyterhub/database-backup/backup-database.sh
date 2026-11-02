#!/bin/sh
# Back up the hub sqlite DB to depot storage, keeping the 14 most recent.
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
# kubectl exec's stdout also carries what the node's runtime prints, so the database
# travels framed, and only bytes matching the pod's checksum become a backup.
TRANSFER="${BACKUP_PATH}.transfer"
PARTIAL="${BACKUP_PATH}.partial"
trap 'rm -f "$TRANSFER" "$PARTIAL"' EXIT

echo "Backing up database from pod ${HUB_POD} to ${BACKUP_PATH}..."
if ! kubectl exec -n cms "$HUB_POD" -- python3 -c '
import base64, hashlib, os, sqlite3, sys, tempfile

src = sqlite3.connect(sys.argv[1], timeout=60)
fd, tmp = tempfile.mkstemp(suffix=".sqlite")
os.close(fd)
try:
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    if check != "ok":
        sys.exit(f"integrity_check: {check}")
    with open(tmp, "rb") as f:
        body = f.read()
    print("BACKUP-BEGIN", hashlib.sha256(body).hexdigest())
    print(base64.encodebytes(body).decode(), end="")
    print("BACKUP-END")
finally:
    os.unlink(tmp)
' "$DB_PATH" >"$TRANSFER"; then
	echo "ERROR: Failed to back up database from pod"
	exit 1
fi

sed -n '/^BACKUP-BEGIN /,/^BACKUP-END$/{/^BACKUP-/d;p;}' "$TRANSFER" | base64 -d >"$PARTIAL"
WANT=$(sed -n 's/^BACKUP-BEGIN //p' "$TRANSFER")
GOT=$(sha256sum "$PARTIAL" | cut -d' ' -f1)
if [ -z "$WANT" ] || [ "$GOT" != "$WANT" ] || ! grep -q '^BACKUP-END$' "$TRANSFER"; then
	echo "ERROR: transferred database does not match the pod's checksum"
	exit 1
fi

mv "$PARTIAL" "$BACKUP_PATH"
echo "Database backup completed successfully: ${BACKUP_PATH}"
echo "Cleaning up old backups (keeping last 14)..."
cd "$BACKUP_DIR"
ls -t jupyterhub-*.sqlite | tail -n +15 | xargs -r rm -f
echo "Current backups:"
ls -la jupyterhub-*.sqlite 2>/dev/null || echo "No backups found"

echo "Database backup job completed at $(date)"
