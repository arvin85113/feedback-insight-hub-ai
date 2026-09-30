from django import forms


class OrganizationSettingsForm(forms.Form):
    name = forms.CharField(label="組織名稱", max_length=120)
