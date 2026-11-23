from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.users.models import Department, User

from . import payments_bridge


class Contribution(models.Model):
    """
    A fee/dues a department asks its students to pay.

    Money rule (BRIEF.md / skills.md): `amount` is always a DecimalField and
    must never be touched by a student. The amount is fixed by the class
    rep/admin who creates the contribution; payment flows (future payments
    app) must read THIS amount, never take one from the client.
    """

    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,  # a department with fees cannot be deleted
        related_name='contributions',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='contributions_created',
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    deadline = models.DateTimeField(null=True, blank=True)
    is_mandatory = models.BooleanField(default=True)
    target_level = models.CharField(
        max_length=10,
        choices=User.LEVEL_CHOICES,
        null=True,
        blank=True,
    )
    # A fee the department has stopped accepting. Closing is ALWAYS preferred
    # over deleting: the `Payment` rows that reference this fee are the audit
    # trail of money actually collected, so a fee that ever took payment must
    # survive. A closed fee disappears for students and can no longer be paid,
    # but stays visible to reps/admins and still counts in every total.
    is_closed = models.BooleanField(default=False, db_index=True)
    # Set when a session rollover has frozen this fee. From then on, who
    # owes it is the recorded list of debtors (`debts`) plus whoever paid —
    # never the current class list, which has moved up a level.
    carried_over = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['department', 'created_at']),
            models.Index(fields=['department', 'deadline']),
            models.Index(fields=['department', 'target_level']),
        ]
        verbose_name = 'Contribution'
        verbose_name_plural = 'Contributions'

    def __str__(self):
        return self.title

    @property
    def is_active(self):
        """A contribution students can still see/pay: open, and deadline ahead."""
        return not self.is_closed and (
            self.deadline is None or self.deadline >= timezone.now()
        )

    @staticmethod
    def open_q(now=None):
        """
        Filter for fees students may still see and pay: not closed, and either
        no deadline or one still ahead. Single source of truth — the student
        list and the payment-initiate gate must never disagree about which fees
        are open, or a stale id could start a payment for a closed collection.
        """
        now = now or timezone.now()
        return Q(is_closed=False) & (Q(deadline__isnull=True) | Q(deadline__gte=now))

    @classmethod
    def payable_by(cls, user):
        """
        Every fee this student can see and pay. Single source of truth for
        the student fee list AND the payment gates, so they cannot disagree.

          * this session's open fees for their department and level, and
          * unpaid mandatory fees carried over from earlier sessions —
            these follow the student whatever level they are in now, and
            even after they graduate.
        """
        owed = Q(carried_over=True, debts__student=user)
        if user.graduated_at or not user.department_id:
            return cls.objects.filter(owed).distinct()
        level = Q(target_level__isnull=True)
        if user.level:
            level |= Q(target_level=user.level)
        current = (
            Q(department_id=user.department_id, carried_over=False)
            & cls.open_q() & level
        )
        return cls.objects.filter(current | owed).distinct()

    def eligible_students(self):
        """Users who owe/see this contribution (department + level + role).

        Class reps and executives ARE students and still pay dues; only
        admins/staff are exempt (owner decision, 2026-09-16). Graduates are
        no longer billed.
        """
        if self.carried_over:
            # Frozen at rollover: the students recorded as still owing, plus
            # everyone who had already paid.
            return User.objects.filter(
                Q(debts__contribution=self)
                | Q(payments__contribution=self, payments__status='success')
            ).distinct()
        qs = User.objects.filter(
            department=self.department,
            role__in=User.DUES_PAYING_ROLES,
            graduated_at__isnull=True,
        )
        if self.target_level:
            qs = qs.filter(level=self.target_level)
        return qs

    # --- Payment-aware helpers (see payments_bridge docstring) ------------

    def has_paid(self, user):
        return payments_bridge.has_paid(self, user)

    def total_expected(self):
        return self.amount * self.eligible_students().count()

    def total_collected(self):
        return payments_bridge.total_collected(self)

    def outstanding_count(self):
        return max(
            self.eligible_students().count() - len(payments_bridge.paid_student_ids(self)),
            0,
        )

class Debt(models.Model):
    """
    A mandatory fee a student had not paid when its session ended.

    Written once, at the session rollover, and never edited or deleted: it is
    the record that this student owed this fee. The debt is settled by an
    ordinary successful Payment for the same student and fee — there is no
    separate "cleared" flag to fall out of step with the money.
    """
    contribution = models.ForeignKey(
        Contribution, on_delete=models.PROTECT, related_name='debts'
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='debts'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['contribution', 'student'], name='unique_debt_per_student_fee'
            ),
        ]

    def __str__(self):
        return f"{self.student} owes {self.contribution}"
