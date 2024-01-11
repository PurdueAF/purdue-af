scp dkondra@hammer.rcac.purdue.edu:*-auth.txt .

files=("purdue-auth.txt" "cern-auth.txt")
namespaces=("cms" "cms-dev")

for namespace in "${namespaces[@]}"; do
    for file in "${files[@]}"; do
        secret_name=$(echo "$file" | sed 's/\.txt//')
        kubectl create secret generic "$secret_name" --from-file="$file" --namespace="$namespace" --dry-run=client -o yaml | kubectl apply -f -
    done
done
