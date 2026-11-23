from rest_framework.permissions import BasePermission

from apps.users.permissions import is_admin  # the one definition of 'admin'


class IsAdminUser(BasePermission):
    """
    Allows access only to admin users.

    Uses the same definition as the rest of the app (an admin role, OR a
    staff/superuser) so there is ONE source of truth for 'admin'.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and is_admin(user))

