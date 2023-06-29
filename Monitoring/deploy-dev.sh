export JSONNET_PATH=grafana-dashboards/vendor/:$JSONNET_PATH
./grafana-dashboards/deploy.py http://grafana.cms-dev.geddes.rcac.purdue.edu:3000 --dashboards-dir my-dashboards/