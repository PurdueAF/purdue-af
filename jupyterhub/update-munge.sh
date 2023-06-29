kubectl delete secret munge-key --ignore-not-found -n cms-dev
kubectl create secret generic munge-key --from-file=munge.key -n cms-dev

kubectl delete secret munge-key --ignore-not-found -n cms
kubectl create secret generic munge-key --from-file=munge.key -n cms
