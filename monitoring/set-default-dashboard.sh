read -p "Namespace: " namespace
read -s -p "Enter admin password: " password
echo

grafana_server=http://grafana.$namespace.geddes.rcac.purdue.edu:3000
dashboards_info=$(curl -XGET $grafana_server/api/search?type=dash-db --user "admin:$password")
dashboard_id=$(echo "$dashboards_info" | jq '.[] | select(.uid == "hub-dashboard") | .id')
response=$(curl $grafana_server/api/preferences/set-home-dash -H "content-type: application/json" --user "admin:$password" -d "{\"homeDashboardId\": $dashboard_id}")
echo
echo "$response" | jq -r '.message'
echo
