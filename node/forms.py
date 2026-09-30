from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError


class OrganizationSettingsForm(forms.Form):
    name = forms.CharField(label="組織名稱", max_length=120)


class NodeSetupForm(forms.Form):
    organization_name = forms.CharField(label="組織名稱", max_length=120)
    email = forms.EmailField(label="擁有者 Email")
    password1 = forms.CharField(label="密碼", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(label="確認密碼", strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise ValidationError("此 Email 已有帳號。")
        return email

    def clean(self):
        cleaned = super().clean()
        password1, password2 = cleaned.get("password1"), cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "兩次輸入的密碼不一致。")
        elif password1 and cleaned.get("email"):
            candidate = get_user_model()(username=cleaned["email"], email=cleaned["email"])
            try:
                password_validation.validate_password(password1, user=candidate)
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned
