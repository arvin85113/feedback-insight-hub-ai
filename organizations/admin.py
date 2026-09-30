from django.contrib import admin

from .models import Organization, OrganizationMembership

admin.site.register(Organization)
admin.site.register(OrganizationMembership)
