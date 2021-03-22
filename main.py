from functools import wraps
import logging
import os
import time

from flask import request
import google.auth
from google.auth import iam
from google.oauth2 import id_token
from google.auth.transport.requests import Request as GRequest
from google.oauth2.service_account import Credentials
import jwt
from requests import Request
from requests import Session


IAM_SCOPE = 'https://www.googleapis.com/auth/iam'
OAUTH_TOKEN_URI = 'https://www.googleapis.com/oauth2/v4/token'
HOST_HEADER = 'Forward-Host'

_oidc_token = None
_session = Session()
_adc_credentials, _ = google.auth.default(scopes=[IAM_SCOPE])

_whitelist = os.getenv('WHITELIST', [])
if _whitelist:
    _whitelist = [p.strip() for p in _whitelist.split(',')]

_username = os.getenv('AUTH_USERNAME')
_password = os.getenv('AUTH_PASSWORD')

# For service accounts using the Compute Engine metadata service, which is the
# case for Cloud Function service accounts, service_account_email isn't
# available until refresh is called.
_adc_credentials.refresh(GRequest())

# Since the Compute Engine metadata service doesn't expose the service
# account key, we use the IAM signBlob API to sign instead. In order for this
# to work, the Cloud Function's service account needs the "Service Account
# Actor" role.
_signer = iam.Signer(
    GRequest(), _adc_credentials, _adc_credentials.service_account_email)


def requires_auth(f):
    """Decorator to enforce Basic authentication on requests."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if _is_auth_enabled():
            if not auth or not _check_auth(auth.username, auth.password):
                return ('Could not verify your access level for that URL.\n'
                        'You have to login with proper credentials.', 401,
                        {'WWW-Authenticate': 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return decorated

@requires_auth
def handle_request(proxied_request):
    """Proxy the given request to the URL in the Forward-Host header with an
    Authorization header set using an OIDC bearer token for the Cloud
    Function's service account. If the header is not present, return a 400
    error.
    """

    host = proxied_request.headers.get(HOST_HEADER)
    if not host:
        return 'Required header {} not present'.format(HOST_HEADER), 400

    scheme = proxied_request.headers.get('X-Forwarded-Proto', 'https')
    url = '{}://{}{}'.format(scheme, host, proxied_request.path)
    headers = dict(proxied_request.headers)

    # Check path against whitelist.
    path = proxied_request.path
    if not path:
        path = '/'
    # TODO: Implement proper wildcarding for paths.
    if '*' not in _whitelist and path not in _whitelist:
        logging.warn('Rejected {} {}, not in whitelist'.format(
            proxied_request.method, url))
        return 'Requested path {} not in whitelist'.format(path), 403

    #global _oidc_token
    #if not _oidc_token or _oidc_token.is_expired():
    #    _oidc_token = _get_google_oidc_token()
    #    logging.info('Renewed OIDC bearer token for {}'.format(
    #        _adc_credentials.service_account_email))

    client_id = os.getenv('CLIENT_ID')
    _oidc_token = id_token.fetch_id_token(GRequest(), client_id)

    # Add the Authorization header with the OIDC token.
    headers['Authorization'] = 'Bearer {}'.format(_oidc_token)

    # We don't want to forward the Host header.
    headers.pop('Host', None)
    request = Request(proxied_request.method, url,
                      headers=headers,
                      data=proxied_request.data)

    # Send the proxied request.
    prepped = request.prepare()
    logging.info('{} {}'.format(prepped.method, prepped.url))
    resp = _session.send(prepped)

    # Strip hop-by-hop headers and Content-Encoding.
    headers = _strip_hop_by_hop_headers(resp.headers)
    headers.pop('Content-Encoding', None)

    return resp.content, resp.status_code, headers.items()

_hoppish = {
    'connection': 1,
    'keep-alive': 1,
    'proxy-authenticate': 1,
    'proxy-authorization': 1,
    'te': 1,
    'trailers': 1,
    'transfer-encoding': 1,
    'upgrade': 1,
}.__contains__


def _is_hop_by_hop(header_name):
    """Return True if 'header_name' is an HTTP/1.1 "Hop-by-Hop" header."""
    return _hoppish(header_name.lower())


def _strip_hop_by_hop_headers(headers):
    """Return a dict with HTTP/1.1 "Hop-by-Hop" headers removed."""
    return {k: v for (k, v) in headers.items() if not _is_hop_by_hop(k)}


def _check_auth(username, password):
    """Validate a username/password combination."""
    return username == _username and password == _password


def _is_auth_enabled():
    """Return True if authentication is enabled, False if not."""
    return _username is not None and _password is not None
