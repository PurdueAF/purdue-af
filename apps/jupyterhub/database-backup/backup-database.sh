#!/bin/sh
# Back up the hub sqlite DB to depot storage, keeping the 14 most recent.
set -eu
umask 077

BACKUP_DIR=/depot/cms/purdue-af/backups
DB_PATH=/srv/jupyterhub/jupyterhub.sqlite

HUB_POD=$(kubectl get pods -n cms -l app=jupyterhub,component=hub \
	-o jsonpath='{.items[0].metadata.name}')

if [ "$(date +%H)" -lt 12 ]; then TIMESTAMP="midnight"; else TIMESTAMP="noon"; fi
BACKUP_FILENAME="jupyterhub-$(date +%Y)$(date +%b | tr '[:upper:]' '[:lower:]')$(date +%d)-${TIMESTAMP}.sqlite"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

mkdir -p "$BACKUP_DIR"
rm -f "$BACKUP_DIR"/jupyterhub-*.sqlite.transfer "$BACKUP_DIR"/jupyterhub-*.sqlite.partial

# SQLite's online backup: a file copy of a database the hub is writing can be torn.
# kubectl exec's stdout also carries what the node's runtime prints, so the database
# travels framed, and only bytes matching the pod's checksum become a backup.
TRANSFER="${BACKUP_PATH}.transfer"
PARTIAL="${BACKUP_PATH}.partial"
trap 'rm -f "$TRANSFER" "$PARTIAL"' EXIT

# Every session mounts this volume, and the directory's defaults can outrank the umask.
: >"$TRANSFER"
: >"$PARTIAL"
chmod 600 "$TRANSFER" "$PARTIAL" || echo "WARNING: could not restrict ${BACKUP_PATH}"

if ! kubectl exec -n cms "$HUB_POD" -- python3 -c '
import base64, hashlib, os, sqlite3, sys, tempfile

src = sqlite3.connect(f"file:{sys.argv[1]}?mode=rw", uri=True, timeout=60)
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
	echo "ERROR: Failed to back up database from pod ${HUB_POD}"
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
cd "$BACKUP_DIR"
ls -t jupyterhub-*.sqlite | tail -n +15 | xargs -r rm -f
ls -la jupyterhub-*.sqlite
