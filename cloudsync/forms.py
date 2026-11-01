from django import forms


from django.conf import settings

from .client import check_api_url


class ConnectForm(forms.Form):
    # Browsers treat a text field followed by a password field as a saved login and fill in the
    # user's e-mail and password; these attributes keep the reconnect form empty until typed into.
    api_url = forms.URLField(label="雲端網址", assume_scheme="https",
                             widget=forms.URLInput(attrs={"autocomplete": "off"}))
    token = forms.CharField(label="裝置權杖", strip=True, widget=forms.PasswordInput(
        attrs={"autocomplete": "new-password", "data-1p-ignore": "true", "data-lpignore": "true"}))

    def clean_api_url(self):
        try:
            return check_api_url(self.cleaned_data["api_url"],
                                 allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
