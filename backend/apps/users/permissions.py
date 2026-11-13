from rest_framework.permissions import BasePermission
from .models import User


class IsStudent(BasePermission):
    """
    Allows access only to authenticated students.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == User.ROLE_STUDENT
        )


class IsClassRep(BasePermission):
    """
    Allows access only to authenticated class reps.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == User.ROLE_CLASS_REP
        )


class IsAdminUser(BasePermission):
    """
    Allows access only to admin users or staff.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role == User.ROLE_ADMIN or request.user.is_staff or request.user.is_superuser)
        )


class IsClassRepOrAdmin(BasePermission):
    """
    Allows access to the people who manage fees: class reps, department
    executives and admins (including staff/superusers). What each may touch
    is narrowed further in the views: a rep to their department AND level,
    an executive to their department.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (
                request.user.role in [User.ROLE_CLASS_REP, User.ROLE_EXECUTIVE, User.ROLE_ADMIN]
                or request.user.is_staff
                or request.user.is_superuser
            )
        )


def is_admin(user):
    """The single definition of 'admin': the role, or Django staff/superuser."""
    return user.role == User.ROLE_ADMIN or user.is_staff or user.is_superuser
