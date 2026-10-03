"""Device tokens are kept in the Windows Credential Manager (keyring), never in the database or .env."""

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "FeedbackInsightHub"


def _username(api_url):
    return f"cloud-token:{api_url.rstrip('/')}"


def save_token(api_url, token):
    keyring.set_password(SERVICE, _username(api_url), token)


def load_token(api_url):
    return keyring.get_password(SERVICE, _username(api_url))


def delete_token(api_url):
    try:
        keyring.delete_password(SERVICE, _username(api_url))
    except PasswordDeleteError:
        pass
