kubectl delete secret html-template --ignore-not-found -n cms
kubectl create secret generic html-template --from-file=login.html -n cms

kubectl delete secret html-template --ignore-not-found -n cms-dev
kubectl create secret generic html-template --from-file=login.html -n cms-dev