# in separate terminal:
# kubectl -n cms port-forward svc/cnpg-cluster-rw 5432:5432

# Fetch superuser credentials
PGUSER="$(kubectl -n cms get secret cnpg-cluster-superuser -o jsonpath='{.data.username}' | base64 -D)"
PGPASS="$(kubectl -n cms get secret cnpg-cluster-superuser -o jsonpath='{.data.password}' | base64 -D)"

# Run pgloader from Docker (use host.docker.internal on macOS)
docker run --rm -v "/Users/kondratyevd/Documents/purdue-af/purdue-af":/data dimitri/pgloader:latest \
	pgloader /data/apps/jupyter/jupyterhub-2025sep16-noon.sqlite \
	postgresql://"$PGUSER":"$PGPASS"@host.docker.internal:5432/jupyterhub

# optional: verify
docker run --rm -e PGPASSWORD="$PGPASS" postgres:16-alpine \
  psql -h host.docker.internal -U "$PGUSER" -d jupyterhub -c '\dt'

# optional: verify on cluster
kubectl run pgtest --rm -i --tty --image=postgres:16 -n cms -- \
  psql "postgresql://postgres@cnpg-cluster-rw.cms.svc.cluster.local:5432/jupyterhub"