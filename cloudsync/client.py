"""HTTP client for the cloud node API with the error classes of spec §8."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from django.conf import settings

from .models import CloudLink
from .tokens import load_token

TRANSIENT = "transient"
UNAUTHORIZED = "unauthorized"
GONE = "gone"
CONFLICT = "conflict"
SEMANTIC = "semantic"
CLIENT = "client"
MAX_BACKOFF_SECONDS = 30 * 60


class NotLinked(Exception):
    pass


class CloudError(Exception):
    def __init__(self, kind, message="", *, status=None, retry_after=None, payload=None):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.payload = payload or {}


def classify(status):
    if status == 401:
        return UNAUTHORIZED
    if status == 409:
        return CONFLICT
    if status == 410:
        return GONE
    if status == 422:
        return SEMANTIC
    if status == 429 or status >= 500:
        return TRANSIENT
    return CLIENT


def parse_retry_after(value):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


def backoff_seconds(failures, retry_after=None):
    base = min(60 * 2 ** max(failures - 1, 0), MAX_BACKOFF_SECONDS)
    return max(base, retry_after or 0)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_api_url(api_url, *, allow_loopback_http=False):
    """Bearer tokens travel only over HTTPS; plain HTTP is allowed solely for loopback in isolated tests."""

    parts = urlsplit((api_url or "").strip())
    if parts.scheme == "https" and parts.hostname:
        return api_url.strip().rstrip("/")
    if parts.scheme == "http" and allow_loopback_http and parts.hostname in LOOPBACK_HOSTS:
        return api_url.strip().rstrip("/")
    raise ValueError("雲端網址必須使用 https://")


class CloudClient:
    def __init__(self, api_url, token, *, session=None, timeout=15, allow_loopback_http=False):
        self.base = check_api_url(api_url, allow_loopback_http=allow_loopback_http) + "/api/node/v1/"
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def request(self, method, path, *, params=None, body=None):
        try:
            response = self.session.request(
                method,
                self.base + path,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                allow_redirects=False,  # never resend the bearer token to another location
            )
        except requests.RequestException as exc:
            raise CloudError(TRANSIENT, str(exc)[:200]) from exc
        if 300 <= response.status_code < 400:
            raise CloudError(CLIENT, "unexpected redirect", status=response.status_code)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise CloudError(
                classify(response.status_code),
                str(payload.get("message") or payload.get("error") or response.status_code),
                status=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
                payload=payload,
            )
        return response.json() if response.content else {}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body=None):
        return self.request("POST", path, body=body if body is not None else {})

    def put(self, path, body):
        return self.request("PUT", path, body=body)


def client_for_link(link=None):
    link = link or CloudLink.load()
    if not link.is_linked:
        raise NotLinked()
    token = load_token(link.api_url)
    if not token:
        raise NotLinked()
    return CloudClient(link.api_url, token, allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
