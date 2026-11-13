from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.utils import timezone


class Department(models.Model):
    name = models.CharField(max_length=150, unique=True)
    faculty = models.CharField(max_length=150)
    # The last level of this department's programme. A new session graduates
    # everyone at (or above) it; everyone else moves up one level.
    final_level = models.CharField(max_length=10, default='400')

    class Meta:
        ordering = ['name']
        verbose_name = 'Department'
        verbose_name_plural = 'Departments'

    def __str__(self):
        return f"{self.name} ({self.faculty})"


class UserManager(DjangoUserManager):
    """Keep a missing email as NULL instead of Django's default ``''``.

    ``BaseUserManager.normalize_email(None)`` returns ``''``, and ``email`` is
    unique — so the second student imported without an email would collide on
    that unique index and abort the whole roster import. NULL is distinct in a
    unique index, so a full class list can be imported with no emails at all.
    """

    # We have no data migrations calling create_user, so there is nothing for
    # Django to freeze into a migration file.
    use_in_migrations = False

    @classmethod
    def normalize_email(cls, email):
        if not email:
            return None
        return super().normalize_email(email)


class User(AbstractUser):
    objects = UserManager()

    ROLE_STUDENT = 'student'
    ROLE_CLASS_REP = 'class_rep'
    # A department executive (e.g. the financial secretary): manages fees and
    # offline payments for the WHOLE department, across levels. Like a class
    # rep, still a student who owes dues.
    ROLE_EXECUTIVE = 'executive'
    ROLE_ADMIN = 'admin'

    # Offices a student can hold and hand over to another student.
    OFFICES = (ROLE_CLASS_REP, ROLE_EXECUTIVE)
    # Everyone who owes dues: admins/staff are the only exemption.
    DUES_PAYING_ROLES = (ROLE_STUDENT, ROLE_CLASS_REP, ROLE_EXECUTIVE)

    ROLE_CHOICES = [
        (ROLE_STUDENT, 'Student'),
        (ROLE_CLASS_REP, 'Class Rep'),
        (ROLE_EXECUTIVE, 'Department Executive'),
        (ROLE_ADMIN, 'Admin'),
    ]

    LEVEL_100 = '100'
    LEVEL_200 = '200'
    LEVEL_300 = '300'
    LEVEL_400 = '400'
    LEVEL_500 = '500'

    LEVEL_CHOICES = [
        (LEVEL_100, '100'),
        (LEVEL_200, '200'),
        (LEVEL_300, '300'),
        (LEVEL_400, '400'),
        (LEVEL_500, '500'),
    ]

    # Nullable so imported students (who may not have an email on file yet)
    # can exist without one; unique still enforced for non-null values.
    email = models.EmailField(unique=True, null=True, blank=True)
    matric_number = models.CharField(max_length=50, unique=True, null=True, blank=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='students'
    )
    level = models.CharField(
        max_length=10,
        choices=LEVEL_CHOICES,
        null=True,
        blank=True
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_STUDENT
    )
    phone_number = models.CharField(max_length=20, blank=True, default='')
    # Set when a new session graduates this student. A graduate keeps their
    # login, payment history and receipts, but is no longer billed.
    graduated_at = models.DateTimeField(null=True, blank=True)
    # The roster import that created this account: only THAT import's claim
    # code can claim it. Null for accounts that predate this field.
    claim_batch = models.ForeignKey(
        'ClaimBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='students',
    )

    class Meta:
        ordering = ['id']
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class ClaimBatch(models.Model):
    """
    One shared claim code per CSV import — the entire credential-distribution
    task for a cohort. Imported accounts have NO password and cannot be logged
    into until the student claims them (matric + first name + this code).
    """
    code = models.CharField(max_length=12, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='claim_batches',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)

    CLAIM_TTL_DAYS = 30

    class Meta:
        ordering = ['-created_at']

    def is_usable(self):
        """Active, and still inside the claim window."""
        return self.is_active and self.created_at >= timezone.now() - timedelta(
            days=self.CLAIM_TTL_DAYS
        )

    def __str__(self):
        return f"{self.code} (active={self.is_active})"


class PasswordResetCode(models.Model):
    """
    Single-use, short-lived code a rep/admin hands to a student in person
    (Option A — no email dependency). The student resets their own password
    with it; the rep never sees or sets the new password.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reset_codes',
    )
    code = models.CharField(max_length=12, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reset_codes_issued',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    RESET_TTL_MINUTES = 30

    class Meta:
        ordering = ['-created_at']

    def is_expired(self):
        return (
            self.used_at is not None
            or self.created_at
            < timezone.now() - timedelta(minutes=self.RESET_TTL_MINUTES)
        )

    def __str__(self):
        return f"{self.user} - {self.code}"



class RoleChange(models.Model):
    """
    Audit trail for who held which office, and who put them there.

    One row per change — an appointment by an admin, each side of a handover,
    or an office lost at graduation. Rows are never edited or deleted.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='role_changes'
    )
    old_role = models.CharField(max_length=20)
    new_role = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='role_changes_made',
    )
    reason = models.CharField(max_length=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user}: {self.old_role} -> {self.new_role} ({self.reason})"


class AcademicSession(models.Model):
    """
    One row per session rollover. The unique name is what makes a rollover
    run-once: starting "2026/2027" twice is refused instead of promoting
    every class a second time.
    """
    name = models.CharField(max_length=20, unique=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+'
    )
    started_at = models.DateTimeField(auto_now_add=True)
    promoted = models.PositiveIntegerField(default=0)
    graduated = models.PositiveIntegerField(default=0)
    fees_closed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return self.name
