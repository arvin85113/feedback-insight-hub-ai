from .models import OrganizationMembership


def organization_role(user):
    """The user's organization-level role ("owner"/"admin"), or None."""

    if not getattr(user, "is_authenticated", False):
        return None
    membership = OrganizationMembership.objects.filter(user=user).only("role").first()
    return membership.role if membership else None
