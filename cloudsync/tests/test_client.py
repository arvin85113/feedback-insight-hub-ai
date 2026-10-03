from django.test import SimpleTestCase, TestCase

from cloudsync.client import (
    CLIENT,
    CONFLICT,
    GONE,
    SEMANTIC,
    TRANSIENT,
    UNAUTHORIZED,
    CloudClient,
    CloudError,
    NotLinked,
    backoff_seconds,
    classify,
    client_for_link,
    parse_retry_after,
)
from cloudsync.models import CloudLink
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import delete_token, load_token, save_token


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = b"x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class ClassificationTests(SimpleTestCase):
    def test_status_classes(self):
        self.assertEqual([classify(s) for s in (401, 404, 409, 410, 422, 429, 500, 503, 400)],
                         [UNAUTHORIZED, CLIENT, CONFLICT, GONE, SEMANTIC, TRANSIENT, TRANSIENT, TRANSIENT, CLIENT])

    def test_retry_after_seconds_and_date(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("soon"))
        self.assertGreaterEqual(parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 0)

    def test_backoff_grows_caps_and_respects_retry_after(self):
        self.assertEqual([backoff_seconds(n) for n in (1, 2, 3, 6, 10)], [60, 120, 240, 1800, 1800])
        self.assertEqual(backoff_seconds(1, retry_after=7200), 7200)


class ClientTests(SimpleTestCase):
    def test_sends_bearer_and_parses_json(self):
        session = FakeSession(FakeResponse(200, {"ok": True}))
        body = CloudClient("https://cloud.example/", "tok", session=session).get("surveys/snapshot/")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("GET", "https://cloud.example/api/node/v1/surveys/snapshot/"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(body, {"ok": True})

    def test_error_carries_kind_payload_and_retry_after(self):
        session = FakeSession(FakeResponse(409, {"error": "version_conflict", "current_version": 3}, {"Retry-After": "5"}))
        with self.assertRaises(CloudError) as caught:
            CloudClient("https://c", "t", session=session).put("surveys/x/", {"a": 1})
        error = caught.exception
        self.assertEqual((error.kind, error.payload["current_version"], error.retry_after), (CONFLICT, 3, 5.0))

    def test_https_is_required_except_explicit_loopback(self):
        from cloudsync.client import check_api_url

        self.assertEqual(check_api_url("https://cloud.example/"), "https://cloud.example")
        for bad in ("http://cloud.example", "http://127.0.0.1:8000", "ftp://x", "cloud.example"):
            with self.subTest(bad), self.assertRaises(ValueError):
                check_api_url(bad)
        self.assertEqual(check_api_url("http://127.0.0.1:8000", allow_loopback_http=True), "http://127.0.0.1:8000")
        with self.assertRaises(ValueError):
            check_api_url("http://10.0.0.5", allow_loopback_http=True)
        with self.assertRaises(ValueError):
            CloudClient("http://cloud.example", "t", session=FakeSession(FakeResponse(200)))

    def test_redirects_are_not_followed(self):
        session = FakeSession(FakeResponse(302, {}, {"Location": "http://elsewhere"}))
        with self.assertRaises(CloudError) as caught:
            CloudClient("https://c", "t", session=session).get("x/")
        self.assertEqual(caught.exception.kind, CLIENT)
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_network_failure_is_transient(self):
        import requests

        with self.assertRaises(CloudError) as caught:
            CloudClient("https://c", "t", session=FakeSession(error=requests.ConnectionError("down"))).get("x/")
        self.assertEqual(caught.exception.kind, TRANSIENT)


class LinkAndTokenTests(TestCase):
    def test_tokens_live_in_the_keyring(self):
        with memory_keyring() as backend:
            save_token("https://c", "secret")
            self.assertEqual(load_token("https://c"), "secret")
            self.assertNotIn("secret", str(CloudLink.load().__dict__))
            delete_token("https://c")
            delete_token("https://c")  # idempotent
            self.assertIsNone(load_token("https://c"))

    def test_generation_guards_stale_writers(self):
        link = CloudLink.relink("https://c", "55555555-5555-5555-5555-555555555555")
        old_generation = link.generation
        self.assertTrue(CloudLink.update_if_current(old_generation, cursor="n:1"))
        CloudLink.unlink()
        self.assertFalse(CloudLink.update_if_current(old_generation, cursor="n:9"))
        fresh = CloudLink.load()
        self.assertEqual((fresh.cursor, fresh.is_linked), ("", False))

    def test_client_for_link_requires_link_and_token(self):
        with memory_keyring():
            with self.assertRaises(NotLinked):
                client_for_link()
            CloudLink.relink("https://c", "55555555-5555-5555-5555-555555555555")
            with self.assertRaises(NotLinked):
                client_for_link()
            save_token("https://c", "tok")
            self.assertEqual(client_for_link().token, "tok")
