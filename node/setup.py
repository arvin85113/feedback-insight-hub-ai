import hashlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from config.node_paths import clear_setup_token
from organizations.models import Organization, OrganizationMembership

from .audit import SETUP_COMPLETED, record
from .models import NodeInstallation


class SetupAlreadyCompleted(Exception):
    pass


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def complete_setup(*, organization_name, email, password, request=None):
    installation = NodeInstallation.load()
    if installation.is_setup_complete:
        raise SetupAlreadyCompleted
    User = get_user_model()
    owner = User.objects.create_user(
        username=email,
        email=email,
        password=password,
        role=User.Role.MANAGER,
        is_staff=True,
        is_superuser=True,
        is_email_verified=True,
    )
    from allauth.account.models import EmailAddress

    EmailAddress.objects.create(user=owner, email=email, primary=True, verified=True)
    organization = Organization.objects.create(name=organization_name)
    OrganizationMembership.objects.create(
        user=owner, organization=organization, role=OrganizationMembership.Role.OWNER
    )
    installation.setup_completed_at = timezone.now()
    installation.save()
    record(SETUP_COMPLETED, request=request, actor=owner, target=organization.name)
    paths = settings.NODE_PATHS
    transaction.on_commit(lambda: clear_setup_token(paths))
    return owner
