"""
The two moments when control and class membership change hands:

  * hand_over()     — an office holder passes their office to another student.
  * start_session() — a new academic session: every class moves up a level,
                      the final-year class graduates, last session's open fees
                      are closed.

Both run in a single database transaction, so they either happen completely
or not at all, and both leave an audit trail (RoleChange / AcademicSession).
Neither touches a Payment row: money history is never rewritten.
"""
from django.db import transaction
from django.db.models import Case, F, Value, When
from django.utils import timezone

from .models import AcademicSession, Department, RoleChange, User

# 100 -> 200 -> ... The top level has no "next": it only ever graduates.
_LEVELS = [value for value, _ in User.LEVEL_CHOICES]
_NEXT_LEVEL = dict(zip(_LEVELS, _LEVELS[1:]))


class LifecycleError(Exception):
    """A handover or rollover that must not happen; the message is user-facing."""


def record_role_change(user, new_role, changed_by, reason):
    """Change a user's role and write the audit row (caller holds the transaction)."""
    if user.role == new_role:
        return
    RoleChange.objects.create(
        user=user, old_role=user.role, new_role=new_role,
        changed_by=changed_by, reason=reason,
    )
    user.role = new_role
    user.save(update_fields=['role'])


@transaction.atomic
def hand_over(holder, successor):
    """
    Pass `holder`'s office (class rep or executive) to `successor`.

    The successor must be an ordinary, claimed, non-graduated student in the
    same department — and, for a class rep, the same level, since that office
    IS the class. The holder returns to being a student in the same step, so
    there is never a moment with two holders or none.
    """
    office = holder.role
    if office not in User.OFFICES:
        raise LifecycleError('You do not hold an office to hand over.')
    if successor is None or successor.pk == holder.pk:
        raise LifecycleError('Choose another student to hand over to.')
    if (
        successor.role != User.ROLE_STUDENT
        or successor.is_staff
        or successor.graduated_at
        or not successor.is_active
        or not successor.has_usable_password()
        or holder.department_id is None
        or successor.department_id != holder.department_id
    ):
        raise LifecycleError(
            'The successor must be an active student in your department '
            'who has claimed their account.'
        )
    if office == User.ROLE_CLASS_REP and successor.level != holder.level:
        raise LifecycleError('A class rep can only hand over to a student in their own level.')

    record_role_change(successor, office, holder, 'handover')
    record_role_change(holder, User.ROLE_STUDENT, holder, 'handover')
    return successor


def _is_final_year(level, department):
    return _LEVELS.index(level) >= _LEVELS.index(department.final_level)


@transaction.atomic
def start_session(name, started_by, dry_run=False):
    """
    Roll the whole institution into a new academic session.

    Per department: students at (or above) its final level graduate and lose
    any office; everyone else moves up one level, class reps moving with their
    class. Every still-open fee is closed, so last session's level-targeted
    fees can never bill the class that has just moved into that level.

    Students with no department or level on file are left untouched and
    counted, so an admin can fix them. Returns the counts; with dry_run
    nothing is written.
    """
    # Imported here: contributions already imports users (no import cycle at load).
    from apps.contributions.models import Contribution

    name = (name or '').strip()
    if not name:
        raise LifecycleError('A session name is required, e.g. "2026/2027".')
    if AcademicSession.objects.filter(name__iexact=name).exists():
        raise LifecycleError(f'Session "{name}" has already been started.')

    enrolled = User.objects.filter(
        role__in=User.DUES_PAYING_ROLES, graduated_at__isnull=True, is_active=True
    )
    counts = {
        'promoted': 0,
        'graduated': 0,
        'skipped_incomplete': enrolled.filter(department__isnull=True).count()
        + enrolled.filter(department__isnull=False, level__isnull=True).count(),
        'fees_closed': Contribution.objects.filter(is_closed=False).count(),
    }
    now = timezone.now()

    for department in Department.objects.all():
        if department.final_level not in _LEVELS:
            raise LifecycleError(
                f'{department.name} has an invalid final level "{department.final_level}".'
            )
        members = enrolled.filter(department=department, level__in=_LEVELS)
        final_levels = [lv for lv in _LEVELS if _is_final_year(lv, department)]
        # Primary keys are pinned BEFORE anything changes: once a 300-level
        # student is moved to 400 they must not also match "graduating".
        graduating_ids = list(
            members.filter(level__in=final_levels).values_list('pk', flat=True)
        )
        continuing_ids = list(
            members.exclude(level__in=final_levels).values_list('pk', flat=True)
        )
        counts['graduated'] += len(graduating_ids)
        counts['promoted'] += len(continuing_ids)
        if dry_run:
            continue

        graduating = User.objects.filter(pk__in=graduating_ids)
        # Office holders who graduate lose the office, with an audit row each.
        for officer in graduating.filter(role__in=User.OFFICES):
            record_role_change(officer, User.ROLE_STUDENT, started_by, 'graduated')
        graduating.update(graduated_at=now)
        # One UPDATE evaluated against the OLD values: nobody is moved twice.
        User.objects.filter(pk__in=continuing_ids).update(level=Case(
            *[When(level=old, then=Value(new)) for old, new in _NEXT_LEVEL.items()],
            default=F('level'),
        ))

    if not dry_run:
        Contribution.objects.filter(is_closed=False).update(is_closed=True)
        AcademicSession.objects.create(
            name=name, started_by=started_by, promoted=counts['promoted'],
            graduated=counts['graduated'], fees_closed=counts['fees_closed'],
        )
    return {'name': name, 'dry_run': dry_run, **counts}
