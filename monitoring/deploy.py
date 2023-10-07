# https://github.com/jupyterhub/grafana-dashboards/blob/main/deploy.py
# BSD 3-Clause License

# Copyright (c) 2020, Yuvi Panda
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.

# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.

# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#!/usr/bin/env python3
import json
import argparse
import os
from glob import glob
from functools import partial
import subprocess
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError
from copy import deepcopy
import re
import ssl

# UID for the folder under which our dashboards will be setup
DEFAULT_FOLDER_UID = '70E5EE84-1217-4021-A89E-1E3DE0566D93'


def grafana_request(endpoint, token, path, data=None, no_tls_verify=False):
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    method = 'GET' if data is None else 'POST'
    req = Request(f'{endpoint}/api{path}', headers=headers, method=method)

    if not isinstance(data, bytes):
        data = json.dumps(data).encode()

    ctx = None

    if no_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    with urlopen(req, data, context=ctx) as resp:
        return json.load(resp)


def ensure_folder(name, uid, api):
    try:
        return api(f'/folders/{uid}')
    except HTTPError as e:
        if e.code == 404:
            # We got a 404 in
            folder = {
                'uid': uid,
                'title': name
            }
            return api('/folders', folder)
        else:
            raise


def build_dashboard(dashboard_path, api, global_dash=False):

    datasources = api("/datasources")
    datasources_names = [ds["name"] for ds in datasources]

    # We pass the list of all datasources because the global dashboards
    # use this information to show info about all datasources in the same panel
    return json.loads(subprocess.check_output(
        [
            "jsonnet", "-J", "vendor", dashboard_path,
            "--tla-code", f"datasources={datasources_names}"
        ]
    ).decode())


def layout_dashboard(dashboard):
    """
    Automatically layout panels.

    - Default to 12x10 panels
    - Reset x axes when we encounter a row
    - Assume 24 unit width

    Grafana autolayout is not available in the API, so we
    have to do those.
    """
    # Make a copy, since we're going to modify this dict
    dashboard = deepcopy(dashboard)
    cur_x = 0
    cur_y = 0
    for panel in dashboard['panels']:
        pos = panel['gridPos']
        pos['h'] = pos.get('h', 10)
        pos['w'] = pos.get('w', 12)
        pos['x'] = cur_x
        pos['y'] = cur_y

        cur_y += pos['h']
        if panel['type'] == 'row':
            cur_x = 0
        else:
            cur_x = (cur_x + pos['w']) % 24

    return dashboard


def deploy_dashboard(dashboard_path, folder_uid, api, global_dash=False):
    db = build_dashboard(dashboard_path, api, global_dash)

    if not db:
        return

    db = layout_dashboard(db)
    # db = populate_template_variables(api, db)

    data = {
        'dashboard': db,
        'folderId': folder_uid,
        'overwrite': True
    }
    api('/dashboards/db', data)


def get_label_values(api, ds_id, template_query):
    """
    Return response to a `label_values` template query

    `label_values` isn't actually a prometheus thing - it is an API call that
    grafana makes. This function tries to mimic that. Useful for populating variables
    in a dashboard
    """
    # re.DOTALL allows the query to be multi-line
    match = re.match(r'label_values\((?P<query>.*),\s*(?P<label>.*)\)', template_query, re.DOTALL)
    query = match.group('query')
    label = match.group('label')
    query = {'match[]': query}
    # Send a request to the backing prometheus datastore
    proxy_url = f'/datasources/proxy/{ds_id}/api/v1/series?{urlencode(query)}'

    metrics = api(proxy_url)['data']
    return sorted(set(m[label] for m in metrics))


def populate_template_variables(api, db):
    """
    Populate options for template variables.

    For list of hubs and similar, users should be able to select a hub from
    a dropdown list. This is not automatically populated by grafana if you are
    using the API (https://community.grafana.com/t/template-update-variable-api/1882/4)
    so we do it here.
    """
    # We're going to make modifications to db, so let's make a copy
    db = deepcopy(db)

    for var in db.get('templating', {}).get('list', []):
        datasources = api("/datasources")
        if var["type"] == "datasource":
            var["options"] = [{"text": ds["name"], "value": ds["name"]} for ds in datasources]

            # default selection: first datasource in list
            if datasources and not var.get("current"):
                var["current"] = {
                    "selected": True,
                    "tags": [],
                    "text": datasources[0]["name"],
                    "value": datasources[0]["name"],
                }
                var["options"][0]["selected"] = True
        elif var['type'] == 'query':
            template_query = var['query']

            # This requires our token to have admin permissions
            # Default to the first datasource
            prom_id = datasources[0]["id"]

            labels = get_label_values(api, prom_id, template_query)
            var["options"] = [{"text": label, "value": label} for label in labels]
            if labels and not var.get("current"):
                # default selection: all current values
                # logical alternative: pick just the first
                var["current"] = {
                    "selected": True,
                    "tags": [],
                    "text": labels[0],
                    "value": labels[0],
                }
                var["options"][0]["selected"] = True

    return db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('grafana_url', help='Grafana endpoint to deploy dashboards to')
    parser.add_argument('--dashboards-dir', default="dashboards", help='Directory of jsonnet dashboards to deploy')
    parser.add_argument('--folder-name', default='JupyterHub Default Dashboards', help='Name of Folder to deploy to')
    parser.add_argument('--folder-uid', default=DEFAULT_FOLDER_UID, help='UID of grafana folder to deploy to')
    parser.add_argument('--no-tls-verify', action='store_true', default=False,
                        help='Whether or not to skip TLS certificate validation')

    args = parser.parse_args()

    grafana_token = os.environ['GRAFANA_TOKEN']

    api = partial(grafana_request, args.grafana_url, grafana_token, no_tls_verify=args.no_tls_verify)
    folder = ensure_folder(args.folder_name, args.folder_uid, api)

    for dashboard in glob(f'{args.dashboards_dir}/*.jsonnet'):
        deploy_dashboard(dashboard, folder['id'], api)
        print(f'Deployed {dashboard}')


if __name__ == '__main__':
    main()
