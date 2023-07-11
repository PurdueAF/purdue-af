read -p "Namespace: " namespace
echo

grafana_server=http://grafana.$namespace.geddes.rcac.purdue.edu:3000
export JSONNET_PATH=grafana-dashboards/vendor/:$JSONNET_PATH
./grafana-dashboards/deploy.py $grafana_server --dashboards-dir dashboards/
