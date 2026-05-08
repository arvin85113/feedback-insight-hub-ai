from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import User


class LoginForm(AuthenticationForm):
    username = forms.CharField(label="帳號")
    password = forms.CharField(label="密碼", widget=forms.PasswordInput)


class CustomerSignUpForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "email", "notification_opt_in")
        labels = {
            "username": "帳號",
            "first_name": "姓名",
            "email": "Email",
            "notification_opt_in": "願意接收改善通知",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].label = "密碼"
        self.fields["password2"].label = "確認密碼"

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.CUSTOMER
        if commit:
            user.save()
        return user


class CustomerProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "organization")
        labels = {
            "first_name": "名字",
            "last_name": "姓氏",
            "email": "Email",
            "organization": "所屬單位",
        }


class CustomerPreferenceForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("notification_opt_in",)
        labels = {
            "notification_opt_in": "接收改善通知",
        }
