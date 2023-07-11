read -p "Namespace: " namespace
read -s -p "Enter admin password: " password
echo

grafana_server=http://grafana.$namespace.geddes.rcac.purdue.edu:3000
random=$(head -c 4 /dev/urandom | base64 | tr -d '=' | tr '+/' '-_')
response=$(curl -X POST -H "Content-Type: application/json" -d "{\"name\":\"apikeycurl-$random\", \"role\": \"Admin\"}" $grafana_server/api/auth/keys --user "admin:$password")
key=$(echo "$response" | jq -r '.key')
export GRAFANA_TOKEN=$key
echo
echo Grafana token updated: $GRAFANA_TOKEN
echo