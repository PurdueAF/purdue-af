#!/bin/bash

# select namespace for deployment
echo
read -p "  > Namespace: " namespace
if [ "$namespace" == "cms" ]; then
    grafana_server=https://cms.geddes.rcac.purdue.edu/grafana
    dashboards_dir=dashboards
elif [ "$namespace" == "cms-dev" ]; then
    grafana_server=http://grafana.cms-dev.geddes.rcac.purdue.edu:3000
    dashboards_dir=dashboards-dev
else
    echo "  > ERROR: unknown namespace "$namespace
    return
fi

if [[ $1 == "-r" ]]; then
    echo "  > Password reset requested."
    echo

    # reset admin passowrd
    read -s -p "  > Enter new admin password: " password
    echo
    pod_selector=$(kubectl get pods -n $namespace -l "app=grafana" -o jsonpath="{.items[0].metadata.name}")
    kubectl exec -n $namespace -it $pod_selector -- grafana cli --homepath "/usr/share/grafana" admin reset-admin-password $password

    # verify that API access works
    http_code=$(curl -w "%{http_code}" -s $grafana_server --user "admin:$password" -o /dev/null)
    if [  $http_code == 200 ]; then
        echo "  > Authentication test successful!"
    else
        echo "  > ERROR: Authentication test failed!"
        echo
        return
    fi

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
        echo "  > API key extracted successfully."
    fi
fi

# deploy dashboards
export JSONNET_PATH=grafana-dashboards/vendor/:grafonnet/vendor/:panels
# export JSONNET_PATH=grafonnet/vendor/:$JSONNET_PATH

folder_name="Purdue Analysis Facility Dashboards"
echo "  > Deploying dashboards..."
python deploy.py $grafana_server --dashboards-dir $dashboards_dir --folder-name "${folder_name}"
echo

# install default (home) dashboard
echo "  > Setting home dashboard..."
dashboards_info=$(curl -s -XGET $grafana_server/api/search?type=dash-db --user "admin:$password")
if [ $namespace == "cms" ]; then
    dashboard_id=$(echo "$dashboards_info" | jq '.[] | select(.uid == "purdue-af-dashboard") | .id')
else
    dashboard_id=$(echo "$dashboards_info" | jq '.[] | select(.uid == "hub-dashboard") | .id')
fi
response=$(curl -s $grafana_server/api/preferences/set-home-dash -H "content-type: application/json" --user "admin:$password" -d "{\"homeDashboardId\": $dashboard_id}")
echo "  > Admin user: "$(echo "$response" | jq -r '.message').
response=$(curl -s -X PUT $grafana_server/api/org/preferences -H "content-type: application/json" --user "admin:$password" -d "{\"homeDashboardId\": $dashboard_id}")
echo "  > Organization: "$(echo "$response" | jq -r '.message').
echo
echo "  > DONE!"
echo
