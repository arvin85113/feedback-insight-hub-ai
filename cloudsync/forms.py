from django import forms


from django.conf import settings

from .client import check_api_url


class ConnectForm(forms.Form):
    api_url = forms.URLField(label="雲端網址", assume_scheme="https")
    token = forms.CharField(label="裝置權杖", widget=forms.PasswordInput, strip=True)

    def clean_api_url(self):
        try:
            return check_api_url(self.cleaned_data["api_url"],
                                 allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
