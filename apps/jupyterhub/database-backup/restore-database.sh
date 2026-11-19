#!/bin/sh
# Load the newest hub backup from depot storage into this cluster's hub, then restart the hub.
set -eu

BACKUP_DIR=/depot/cms/purdue-af/backups
DB_PATH=/srv/jupyterhub/jupyterhub.sqlite

# A restored database names other users' servers; a running session could be adopted by one.
if [ -n "$(kubectl get pods -n cms -l component=singleuser-server -o name)" ]; then
	echo "Sessions are running; skipping the restore."
	exit 0
fi

BACKUP=$(ls -t "$BACKUP_DIR"/jupyterhub-*.sqlite | head -n 1)
HUB_POD=$(kubectl get pods -n cms -l app=jupyterhub,component=hub \
	-o jsonpath='{.items[0].metadata.name}')
echo "Restoring ${BACKUP} into ${HUB_POD}"

kubectl exec -i -n cms "$HUB_POD" -- python3 -c '
import hashlib, os, sqlite3, sys, tempfile

body = sys.stdin.buffer.read()
if hashlib.sha256(body).hexdigest() != sys.argv[2]:
    sys.exit("received database does not match the backup checksum")
fd, tmp = tempfile.mkstemp(suffix=".sqlite")
try:
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    src = sqlite3.connect(tmp)
    check = src.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        sys.exit(f"integrity_check: {check}")
    dst = sqlite3.connect(sys.argv[1], timeout=60)
    src.backup(dst)
    dst.close()
    src.close()
finally:
    os.unlink(tmp)
' "$DB_PATH" "$(sha256sum "$BACKUP" | cut -d' ' -f1)" <"$BACKUP"

# The hub holds the old database's state in memory until it restarts.
kubectl delete pod -n cms "$HUB_POD" --wait=false
