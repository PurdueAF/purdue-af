#!/bin/bash

# select namespace for deployment
echo
read -p "  > Namespace: " namespace
if [ "$namespace" != "cms" ] && [ "$namespace" != "cms-dev" ]; then
    echo "  > ERROR: unknown namespace "$namespace
    return
fi
grafana_server=http://grafana.$namespace.geddes.rcac.purdue.edu:3000

# authorize access to Grafana API
read -s -p "  > Enter admin password: " password
echo

# extract API key
random=$(head -c 4 /dev/urandom | base64 | tr -d '=' | tr '+/' '-_')
response=$(curl -s -XPOST -H "Content-Type: application/json" -d "{\"name\":\"apikeycurl-$random\", \"role\": \"Admin\"}" $grafana_server/api/auth/keys --user "admin:$password")
key=$(echo "$response" | jq -r '.key')
if [ -z "$key" ]; then
    echo
    echo "  > ERROR: empty API key!"
    return
else
    export GRAFANA_TOKEN=$key
    echo
    echo "  > API key extracted successfully!"
    echo
fi

# deploy dashboards
export JSONNET_PATH=grafana-dashboards/vendor/:$JSONNET_PATH
echo "  > Deploying dashboards..."
./grafana-dashboards/deploy.py $grafana_server --dashboards-dir dashboards-$namespace/
echo

# install default (home) dashboard
echo "  > Setting home dashboard..."
dashboards_info=$(curl -s -XGET $grafana_server/api/search?type=dash-db --user "admin:$password")
dashboard_id=$(echo "$dashboards_info" | jq '.[] | select(.uid == "hub-dashboard") | .id')
response=$(curl -s $grafana_server/api/preferences/set-home-dash -H "content-type: application/json" --user "admin:$password" -d "{\"homeDashboardId\": $dashboard_id}")
echo "  > "$(echo "$response" | jq -r '.message').
echo
echo "  > DONE!"
echo
