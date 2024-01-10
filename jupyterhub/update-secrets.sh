kubectl apply -n cms -f auth-secret.yaml
kubectl delete secret munge-key --ignore-not-found -n cms
kubectl create secret generic munge-key --from-file=munge.key -n cms


kubectl apply -n cms-dev -f auth-secret-dev.yaml
kubectl delete secret munge-key --ignore-not-found -n cms-dev
kubectl create secret generic munge-key --from-file=munge.key -n cms-dev
