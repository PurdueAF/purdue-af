scp dkondra@cms-fe01.rcac.purdue.edu:purdue-auth.txt .

# files=("purdue-auth.txt" "cern-auth.txt")
# namespaces=("cms" "cms-dev")

# for namespace in "${namespaces[@]}"; do
#     for file in "${files[@]}"; do
#         secret_name=$(echo "$file" | sed 's/\.txt//')
#         kubectl create secret generic "$secret_name" --from-file="$file" --namespace="$namespace" --dry-run=client -o yaml | kubectl apply -f -
#     done
# done

kubectl delete secret purdue-auth --ignore-not-found -n cms
kubectl delete secret cern-auth --ignore-not-found -n cms

kubectl create secret generic purdue-auth --from-file=purdue-auth.txt -n cms
kubectl create secret generic cern-auth --from-file=cern-auth.txt -n cms

kubectl delete secret purdue-auth --ignore-not-found -n cms-dev
kubectl delete secret cern-auth --ignore-not-found -n cms-dev

kubectl create secret generic purdue-auth --from-file=purdue-auth.txt -n cms-dev
kubectl create secret generic cern-auth --from-file=cern-auth.txt -n cms-dev
