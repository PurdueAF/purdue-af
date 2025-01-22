helm repo add ssl-hep https://ssl-hep.github.io/ssl-helm-charts/
helm repo update
helm upgrade --install -f values.yaml servicex ssl-hep/servicex -n cms