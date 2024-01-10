from ldap3 import NTLM, SIMPLE, Server, Connection, ALL, SUBTREE
import json
import os
import contextlib
from kubernetes_asyncio import client

NAMESPACE = os.environ["POD_NAMESPACE"]

def ldap_lookup(username):
    url = "geddes-aux.rcac.purdue.edu"
    searchid = username.split('@')[0]
    baseDN = "ou=People,dc=rcac,dc=purdue,dc=edu"
    search_filter = "(uid={0}*)"
    #username = searchid[0]
    attrs = ['uidNumber','gidNumber']
    s = Server(host=url, use_ssl=True, get_info='ALL')
    #conn = Connection(s, user= DN, password= secret, auto_bind= True, version= 3, authentication='ANONYMOUS', \
    #client_strategy= 'SYNC', auto_referrals= True, check_names= True, read_only= False, lazy= False, raise_exceptions= False)
    s = Server(host= url ,use_ssl= True, get_info= 'ALL')
    conn = Connection(s, version = 3, authentication = "ANONYMOUS")
    conn.start_tls()
    print(conn.result)
    print(conn)
    conn.search(search_base = baseDN, search_filter = search_filter.format(username), search_scope = SUBTREE, attributes = attrs)
    ldap_result_id = json.loads(conn.response_to_json())
    print(ldap_result_id)
    result = ldap_result_id[u'entries'][0][u'attributes']
    uid_number = result[u'uidNumber']
    gid_number = result [u'gidNumber']
    print("UID",+ uid_number)
    print("GID", + gid_number)            
    return uid_number, gid_number

def passthrough_auth_state_hook(spawner, auth_state):
    import pprint
    spawner.userdata = { "name": auth_state['name'],
                        "domain": auth_state['domain']
                        }
    print("GOT STATE:")
    pprint.pprint(spawner.userdata)
    domain = spawner.userdata['domain']
    username = spawner.userdata['name']
    spawner.environment["NB_USER"] = username
    if domain == "purdue.edu":
        uid,gid = ldap_lookup(username)
        spawner.environment["NB_UID"] = str(uid)
        spawner.environment["NB_GID"] = str(gid)
    elif NAMESPACE=="cms":
        # in prod instance do the user mapping
        af_id = int(spawner.user.id)
        if af_id > 199:
            raise Exception(
                f"Error while trying to create an external user with AF ID {af_id}."
                "We ran out of accounts for external users!"
            )
        username = 'paf{:04d}'.format(af_id)
        uid, gid = ldap_lookup(username)
        spawner.environment["NB_UID"] = str(uid)
        spawner.environment["NB_GID"] = str(gid)
    else:
        # in dev instance skip user mapping
        spawner.environment["NB_UID"] = "1000"
        spawner.environment["NB_GID"] = "1000"


async def my_pre_spawn_hook(spawner):
    proxy_name = f"proxy-{spawner.user.name}"
    api = client.CoreV1Api(client.ApiClient())
    dask_proxy = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=proxy_name,
            annotations={
                "metallb.universe.tf/address-pool": "geddes-private-pool"
            },
            labels={"scrape_metrics": "true"}
        ),
        spec=client.V1ServiceSpec(
            selector={
                "hub.jupyter.org/username": spawner.user.name
            },
            ports=[
                client.V1ServicePort(
                    protocol="TCP",
                    port=8786,
                    name="dask-scheduler",
                    target_port=8786
                ),
                client.V1ServicePort(
                    protocol="TCP",
                    port=8787,
                    name="dask-metrics",
                    target_port=8787
                )
            ],
            type="LoadBalancer"
        )
    )
    with contextlib.suppress(client.exceptions.ApiException):
        await api.delete_namespaced_service(proxy_name, NAMESPACE)

    await api.create_namespaced_service(NAMESPACE, dask_proxy)
    spawner.log.info('Dask proxy configured for %s' % spawner.user.name)

async def my_post_stop_hook(spawner):
    proxy_name = f"proxy-{spawner.user.name}"
    api = client.CoreV1Api(client.ApiClient())
    with contextlib.suppress(client.exceptions.ApiException):
        await api.delete_namespaced_service(proxy_name, NAMESPACE)
        spawner.log.info('Dask proxy deleted for %s' % spawner.user.name)

c.KubeSpawner.auth_state_hook = passthrough_auth_state_hook
c.KubeSpawner.pre_spawn_hook = my_pre_spawn_hook
c.KubeSpawner.post_stop_hook = my_post_stop_hook
c.KubeSpawner.notebook_dir = "~"
c.KubeSpawner.working_dir = "/home/{legacy_escape_username}"
c.KubeSpawner.disable_user_config = True
c.KubeSpawner.http_timeout = 150
c.KubeSpawner.start_timeout = 150
c.KernelSpecManager.ensure_native_kernel = False
c.JupyterHub.authenticate_prometheus = False