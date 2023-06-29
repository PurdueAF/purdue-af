scp dkondra@hammer.rcac.purdue.edu:*-auth.txt .

kubectl delete secret purdue-auth --ignore-not-found -n cms
kubectl delete secret cern-auth --ignore-not-found -n cms

kubectl create secret generic purdue-auth --from-file=purdue-auth.txt -n cms
kubectl create secret generic cern-auth --from-file=cern-auth.txt -n cms

kubectl delete secret purdue-auth --ignore-not-found -n cms-dev
kubectl delete secret cern-auth --ignore-not-found -n cms-dev

kubectl create secret generic purdue-auth --from-file=purdue-auth.txt -n cms-dev
kubectl create secret generic cern-auth --from-file=cern-auth.txt -n cms-dev