# Authenticator:
#   admin_users: ['dkondra']
# PurdueCMSIAMAuthenticator:
#   login_service: CMS SSO
#   token_url: https://cms-auth.web.cern.ch/token
#   authorize_url: https://cms-auth.web.cern.ch/authorize
#   oauth_callback_url: https://cmsdev.geddes.rcac.purdue.edu/hub/cms_iam/oauth_callback
#   client_id: 3abe02cd-7baf-43f2-8b54-490d50a76e86
#   client_secret: E-or3GiYQLi0j7w1_Uacs5LGaaHQaQt5toW0ZsVAvSQ76pAlfN7gFPz_Bmk9bd_e5YkIFrCSO6n9Cnpdo161qA
#   userdata_method: GET
#   userdata_params:
#     state: state
#   userdata_url: https://cms-auth.web.cern.ch/userinfo
#   username_key: email
### CERN auth application:
# id: purdue-af-dev
# secret: E9BmlmBUtTD9Lm70yK7O8imnGh0JmhLH

from traitlets import List
from jupyterhub.auth import Authenticator
from oauthenticator.generic import GenericOAuthenticator
from oauthenticator.google import GoogleOAuthenticator
from oauthenticator.azuread import AzureAdOAuthenticator
from oauthenticator.cilogon import CILogonOAuthenticator
from tornado import web

class PurdueCMSIAMAuthenticator(GenericOAuthenticator):
    async def authenticate(self, handler, data=None):
        import pprint
        ret = await super().authenticate(handler, data)
        print("in auth:")
        pprint.pprint(ret)
        name = ret['name']
        username, domain = ret['auth_state']['oauth_user']['email'].split("@")
        fixedUsername = None

        fixedUsername = username + "-cern"
        # with open('/etc/secrets/cern-auth/cern-auth.txt') as file:
        #   if not f"{username}\n" in file.readlines():
        #     raise web.HTTPError(500, "Access denied! Only CMS members are allowed to log in with CERN credentials.")
        
        ret['name'] = fixedUsername
        ret['domain'] = domain
        return ret

class PurdueCILogonOAuthenticator(CILogonOAuthenticator):
    async def authenticate(self, handler, data=None):
        import pprint
        ret = await super().authenticate(handler, data)
        print("in auth:")
        pprint.pprint(ret)
        name = ret['name']
        username, domain = ret['auth_state']['cilogon_user']['eppn'].split("@")
        fixedUsername = None

        if domain == 'purdue.edu':
            fixedUsername = username
            with open('/etc/secrets/purdue-auth/purdue-auth.txt') as file:
                if not f"{username}\n" in file.readlines():
                    raise web.HTTPError(500, f"Access denied! User {username} is not in the list of authorized users.")

        elif domain == 'cern.ch':
            fixedUsername = username + "-cern"
            with open('/etc/secrets/cern-auth/cern-auth.txt') as file:
                if not f"{username}\n" in file.readlines():
                    raise web.HTTPError(500, "Access denied! Only CMS members are allowed to log in with CERN credentials.")
        
        elif domain == 'fnal.gov':
                fixedUsername = username + "-fnal"
        else:
            raise web.HTTPError(500, "Failed to get username from CILogon")
        
        ret['name'] = fixedUsername
        ret['domain'] = domain
        return ret


class PackedAuthenticator(Authenticator):
    authenticators = List(help="The sub-authenticators to use", config=True)

    def __init__(self, *arg, **kwargs):
        super().__init__(*arg, **kwargs)
        self._authenticators = []
        for auth_class, url_scope, configs in self.authenticators:
            instance = auth_class(**configs)
            # get the login url for this authenticator, e.g. 'login' for PAM, 'oauth_login' for Google
            login_url = instance.login_url('')

            # update the login_url function on the instance to fix it as we are adding url_scopes
            instance._url_scope = url_scope
            instance._login_url = login_url
            
            def custom_login_url(self, base_url):
                return url_path_join(base_url, self._url_scope, self._login_url)
            
            instance.login_url = custom_login_url.__get__(instance, auth_class)
            self._authenticators.append({
                'instance': instance,
                'url_scope': url_scope,
            })

    def get_handlers(self, app):
        routes = []
        for _auth in self._authenticators:
            for path, handler in _auth['instance'].get_handlers(app):

                class SubHandler(handler):
                    authenticator = _auth['instance']

                routes.append((f'{_auth["url_scope"]}{path}', SubHandler))
        print("routes", routes)
        return routes
    
    def get_custom_html(self, base_url):
        html = [
        '<div class="service-login">',
        '<h2>Please sign in below</h2>',
        ]
        for authenticator in self._authenticators:
            login_service = authenticator['instance'].login_service or "Local User"
            url = authenticator['instance'].login_url(base_url)

            html.append(
                f"""
                <div style="margin-bottom:10px;">
                <a style="width:20%;" role="button" class='btn btn-jupyter btn-lg' href='{url}'>
                Sign in with {login_service}
                </a>
                </div>
                """
            )
        footer_html = [
        '</div>',
        ]
        return '\n'.join(html + footer_html)

c.PackedAuthenticator.authenticators = [
(PurdueCILogonOAuthenticator, '/cilogon', c['PurdueCILogonOAuthenticator']),
(PurdueCILogonOAuthenticator, '/cilogon1', c['PurdueCILogonOAuthenticator']),
(PurdueCMSIAMAuthenticator, '/cms_iam', c['PurdueCMSIAMAuthenticator']),
]

def passthrough_post_auth_hook(authenticator, handler, authentication):
    import pprint
    print("in post auth:")
    pprint.pprint(authentication)
    if authentication['auth_state'] is None:
        authentication['auth_state'] = {}
    authentication['auth_state']['name'] = authentication['name']
    authentication['auth_state']['domain'] = authentication['domain']
    return authentication

c.JupyterHub.authenticator_class = PackedAuthenticator
c.PackedAuthenticator.post_auth_hook = passthrough_post_auth_hook
