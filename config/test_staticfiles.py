from tempfile import TemporaryDirectory

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from whitenoise.middleware import WhiteNoiseMiddleware


class StaticFilesDeploymentTests(SimpleTestCase):
    def test_whitenoise_serves_source_css_without_collected_assets(self):
        with TemporaryDirectory() as static_root, override_settings(
            DEBUG=False,
            STATIC_ROOT=static_root,
            WHITENOISE_AUTOREFRESH=False,
            WHITENOISE_USE_FINDERS=True,
        ):
            middleware = WhiteNoiseMiddleware(
                lambda request: HttpResponse("not found", status=404)
            )
            response = middleware(RequestFactory().get("/static/css/app.css"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/css"))
