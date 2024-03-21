from ldap3 import Server, Connection, SUBTREE
import json
import os

NAMESPACE = os.environ["POD_NAMESPACE"]

def ldap_lookup(username):
    url = "geddes-aux.rcac.purdue.edu"
    baseDN = "ou=People,dc=rcac,dc=purdue,dc=edu"
    search_filter = "(uid={0}*)"
    attrs = ['uidNumber','gidNumber']
    s = Server(host= url ,use_ssl= True, get_info= 'ALL')
    conn = Connection(s, version = 3, authentication = "ANONYMOUS")
    conn.start_tls()
    # print(conn.result)
    # print(conn)
    conn.search(
        search_base = baseDN,
        search_filter = search_filter.format(username),
        search_scope = SUBTREE,
        attributes = attrs
    )
    ldap_result_id = json.loads(conn.response_to_json())
    print(ldap_result_id)
    result = ldap_result_id[u'entries'][0][u'attributes']
    uid_number = result[u'uidNumber']
    gid_number = result [u'gidNumber']
    print("UID",+ uid_number)
    print("GID", + gid_number)            
    return uid_number, gid_number

def passthrough_auth_state_hook(spawner, auth_state):
    # import pprint
    spawner.userdata = { "name": auth_state['name'],
                        "domain": auth_state['domain']
                        }
    # print("GOT STATE:")
    # pprint.pprint(spawner.userdata)
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

c.KubeSpawner.auth_state_hook = passthrough_auth_state_hook
c.KubeSpawner.notebook_dir = "~"
c.KubeSpawner.working_dir = "/home/{legacy_escape_username}"
c.KubeSpawner.disable_user_config = True
c.KubeSpawner.http_timeout = 210
c.KubeSpawner.start_timeout = 210
c.KernelSpecManager.ensure_native_kernel = False
c.JupyterHub.authenticate_prometheus = False