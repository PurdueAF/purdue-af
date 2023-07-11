random=$(head -c 4 /dev/urandom | base64 | tr -d '=' | tr '+/' '-_')
read -s -p "Enter admin password: " password
echo
ret=$(curl -X POST -H "Content-Type: application/json" -d "{\"name\":\"apikeycurl-$random\", \"role\": \"Admin\"}" http://admin:admin@grafana.cms-dev.geddes.rcac.purdue.edu:3000/api/auth/keys --user "admin:$password")
key=$(echo "$ret" | grep -o '"key":"[^"]*' | sed 's/"key":"//')
export GRAFANA_TOKEN=$key
echo Grafana token updated: $GRAFANA_TOKEN
