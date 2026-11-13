from rest_framework.permissions import BasePermission


class IsAdminUser(BasePermission):
    """
    Allows access only to admin users.

    Uses the same definition as the rest of the app (an admin role, OR a
    staff/superuser) so there is ONE source of truth for 'admin'.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and is_admin(user))


def is_admin(user):
    """The single definition of 'admin': the role, or Django staff/superuser."""
    return user.role == 'admin' or user.is_staff or user.is_superuser