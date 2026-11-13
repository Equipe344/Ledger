import csv
import io

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import get_random_string
from rest_framework import status, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.authtoken.models import Token

from .lifecycle import LifecycleError, hand_over, record_role_change, start_session
from .models import AcademicSession, ClaimBatch, Department, PasswordResetCode, User
from .permissions import IsAdminUser, IsClassRepOrAdmin, is_admin
from .throttles import AccountThrottle
from .serializers import (
    DepartmentSerializer,
    UserRegistrationSerializer,
    LoginSerializer,
    UserProfileSerializer,
)


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def create(self, request, *args, **kwargs):
        # When the roster is the source of truth, students claim their
        # imported account instead; open sign-up would let anyone register a
        # real student's matric number before the roster arrives.
        if not settings.ALLOW_SELF_REGISTRATION:
            return Response(
                {
                    'error': 'registration_closed',
                    'message': 'Sign-up is closed. Claim your account with the code from your department.',
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().create(request, *args, **kwargs)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, AccountThrottle]
    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        # Rotate the token on every login so a leaked token cannot outlive it.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)
        return Response(
            {
                "token": token.key,
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                }
            },
            status=status.HTTP_200_OK
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        try:
            request.user.auth_token.delete()
        except (AttributeError, Token.DoesNotExist):
            pass
        return Response(
            {"message": "Successfully logged out."},
            status=status.HTTP_200_OK
        )


class CurrentUserView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileSerializer

    def get_object(self):
        return self.request.user


class DepartmentListView(generics.ListCreateAPIView):
    """GET is public (signup/registration dropdowns need it without a token);
    POST is admin-only. Departments used to be creatable only in the Django
    admin, so a fresh deploy had none — which blocked fee creation, blocked
    student registration, and made roster imports fail. This closes that gap
    from inside the app.
    """

    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAdminUser()]
        return [AllowAny()]


def _new_code(length=10):
    # Unambiguous alphabet: no O/0, I/1, L/1 confusion when read aloud.
    return get_random_string(
        length, allowed_chars='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    )


IMPORT_MAX_BYTES = 1_000_000
IMPORT_MAX_ROWS = 5000


class ImportStudentsView(APIView):
    """
    POST /api/auth/import/ — admin-only bulk registration from a CSV.

    Header row required: first_name, last_name, matric_number, level,
    department (+ optional email). Existing matric numbers are reported,
    never duplicated. Created accounts are role=student with NO password —
    students claim their own account via /api/auth/claim/ (matric + first
    name + batch code), so nothing is distributed per student even at 2,000+
    rows. dry_run=true validates the file without writing.
    """

    permission_classes = [IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return Response(
                {'error': 'bad_request', 'message': 'file (CSV) is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        dry_run = str(request.data.get('dry_run', '')).lower() in ('1', 'true', 'yes')

        if upload.size > IMPORT_MAX_BYTES:
            return Response(
                {'error': 'bad_request', 'message': 'File is too large (1 MB maximum). Split it by class.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw = upload.read()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1252', errors='replace')  # Excel exports

        reader = csv.DictReader(io.StringIO(text))
        # Only name + matric number are guaranteed to be in a department's
        # records (owner decision): everything else may be absent and is
        # filled in later by the student. Required COLUMNS are therefore just
        # these two — a file without a department/level column is still valid.
        required = {'first_name', 'matric_number'}
        headers = {(h or '').strip().lower() for h in (reader.fieldnames or []) if h}
        missing = required - headers
        if missing:
            return Response(
                {
                    'error': 'bad_request',
                    'message': 'Missing CSV columns: %s.' % ', '.join(sorted(missing)),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = {
            'created': 0, 'skipped_existing': 0,
            'errors': [], 'errors_total': 0, 'dry_run': dry_run,
        }
        dept_cache = {}
        seen_matrics = set()
        seen_emails = set()
        pending_rows = []
        # Loaded once, not queried per row: a 2,000-row roster costs three
        # queries here instead of several thousand.
        users = User.objects.all()
        existing_matrics = {
            m.casefold() for m in users.exclude(matric_number__isnull=True)
            .values_list('matric_number', flat=True)
        }
        existing_usernames = {
            u.casefold() for u in users.values_list('username', flat=True)
        }
        existing_emails = {
            e.casefold() for e in users.exclude(email__isnull=True)
            .values_list('email', flat=True)
        }

        for row_number, raw_row in enumerate(reader, start=2):
            if row_number > IMPORT_MAX_ROWS + 1:
                return Response(
                    {
                        'error': 'bad_request',
                        'message': f'Too many rows ({IMPORT_MAX_ROWS} maximum). Split the file by class.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            row = {
                (k or '').strip().lower(): (v or '').strip()
                for k, v in raw_row.items()
                if k is not None
            }
            matric = row.get('matric_number', '')
            errors = []
            if not matric:
                errors.append('matric_number is required')
            if not row.get('first_name'):
                errors.append('first_name is required')
            # department/level are optional: a partial roster still imports,
            # and the student completes their profile on first login. A name
            # that IS given but matches no department is still an error (typo
            # guard) — see below.
            level = row.get('level', '')
            if level and level not in dict(User.LEVEL_CHOICES):
                errors.append('invalid level: %s' % level)
            email = row.get('email', '') or None
            if len(matric) > 50:
                errors.append('matric_number is too long (50 characters maximum)')
            if len(row.get('first_name', '')) > 150 or len(row.get('last_name', '')) > 150:
                errors.append('name is too long (150 characters maximum)')
            if email:
                try:
                    validate_email(email)
                except DjangoValidationError:
                    errors.append('invalid email: %s' % email)

            department = None
            dept_name = row.get('department', '')
            if dept_name:
                if dept_name not in dept_cache:
                    dept_cache[dept_name] = Department.objects.filter(
                        name__iexact=dept_name
                    ).first()
                department = dept_cache[dept_name]
                if department is None:
                    errors.append('unknown department: %s' % dept_name)

            if matric:
                key = matric.casefold()
                if key in seen_matrics:
                    errors.append('duplicate matric_number inside this file')
                seen_matrics.add(key)
                if key in existing_matrics:
                    errors.append('a user with this matric_number already exists')
                    results['skipped_existing'] += 1
                elif key in existing_usernames:
                    errors.append('that matric_number is already taken as a username')
            if email:
                ekey = email.casefold()
                if ekey in seen_emails:
                    errors.append('duplicate email inside this file')
                seen_emails.add(ekey)
                if ekey in existing_emails:
                    errors.append('a user with this email already exists')

            if errors:
                results['errors_total'] += 1
                if len(results['errors']) < 50:
                    results['errors'].append(
                        {'row': row_number, 'matric_number': matric, 'errors': errors}
                    )
                continue

            pending_rows.append({
                'first_name': row.get('first_name', ''),
                'last_name': row.get('last_name', ''),
                'matric_number': matric,
                'level': level or None,
                'department': department,
                'email': email,
            })

        if dry_run:
            results['would_create'] = len(pending_rows)
            return Response(results, status=status.HTTP_200_OK)

        if not pending_rows:
            # Nothing to claim, so no claim code is minted.
            return Response(results, status=status.HTTP_200_OK)

        # All or nothing: a failure on row 900 must not leave 899 accounts
        # behind with no claim code to hand out.
        with transaction.atomic():
            batch = ClaimBatch.objects.create(code=_new_code(10), created_by=request.user)
            for data in pending_rows:
                # password=None -> unusable: the account is inert until the
                # student claims it (matric + first name + THIS batch's code).
                User.objects.create_user(
                    username=data['matric_number'],
                    email=data['email'],
                    password=None,
                    first_name=data['first_name'],
                    last_name=data['last_name'],
                    matric_number=data['matric_number'],
                    department=data['department'],
                    level=data['level'],
                    role=User.ROLE_STUDENT,
                    claim_batch=batch,
                )
        results['created'] = len(pending_rows)
        results['claim_batch_code'] = batch.code
        results['claim_code_valid_days'] = ClaimBatch.CLAIM_TTL_DAYS
        return Response(results, status=status.HTTP_200_OK)


class ClaimAccountView(APIView):
    """
    POST /api/auth/claim/ — a student sets the first password on their
    imported account (matric + first name + shared batch code).

    Every pre-password failure returns the SAME generic error, so this
    endpoint can never be used to probe which matric numbers exist.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, AccountThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        matric = (request.data.get('matric_number') or '').strip()
        first_name = (request.data.get('first_name') or '').strip()
        batch_code = (request.data.get('batch_code') or '').strip()
        password = request.data.get('password') or ''

        generic_fail = {
            'error': 'claim_failed',
            'message': 'Claim failed. Check your matric number, first name and claim code.',
        }
        if not (matric and first_name and batch_code and password):
            return Response(generic_fail, status=status.HTTP_400_BAD_REQUEST)

        batch = ClaimBatch.objects.filter(
            code__iexact=batch_code, is_active=True
        ).first()
        user = User.objects.filter(matric_number__iexact=matric).first()
        name_ok = (
            user is not None
            and user.first_name.strip().casefold() == first_name.casefold()
        )
        # The code must still be live AND be the one issued with this
        # student's own import, so a code from one class cannot claim an
        # account in another. (Accounts imported before batches were linked
        # have no batch on file and accept any live code.)
        batch_ok = (
            batch is not None
            and batch.is_usable()
            and user is not None
            and user.claim_batch_id in (None, batch.id)
        )
        if not batch_ok or not name_ok or user.has_usable_password():
            return Response(generic_fail, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_password(password, user=user)
        except Exception as exc:
            return Response(
                {'error': 'weak_password', 'message': ' '.join(exc.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = (request.data.get('email') or '').strip() or None
        if email and User.objects.filter(
            email__iexact=email
        ).exclude(pk=user.pk).exists():
            return Response(
                {'error': 'email_taken', 'message': 'That email is already in use.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if email:
            # Online payments need a reachable address; imported students may
            # not have had one on file.
            user.email = email
        user.set_password(password)
        user.save()

        return Response(
            {
                'message': 'Account claimed successfully. You can now log in.',
                'username': user.username,
            },
            status=status.HTTP_200_OK,
        )


class IssueResetCodeView(APIView):
    """
    POST /api/auth/reset-code/ — rep/admin: hand a one-time reset code to a
    student in person (Option A, no email dependency). Codes expire in 30
    minutes, are single-use, and the student sets the new password
    themselves — the rep never sees it.
    """

    permission_classes = [IsClassRepOrAdmin]

    def post(self, request):
        matric = (request.data.get('matric_number') or '').strip()
        user = User.objects.filter(matric_number__iexact=matric).first() if matric else None
        if user is None:
            return Response(
                {'error': 'not_found', 'message': 'No user with that matric number.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        # Admins can reach anyone. A rep can only reset an ordinary STUDENT in
        # their own department and level — never another rep or an admin, or a
        # reset code would be a way to take over an account with more power.
        # An executive covers the whole department and may also reset a class
        # rep, but never another executive.
        requester = request.user
        is_executive = requester.role == User.ROLE_EXECUTIVE
        resettable = (
            (User.ROLE_STUDENT, User.ROLE_CLASS_REP) if is_executive
            else (User.ROLE_STUDENT,)
        )
        if not is_admin(requester) and (
            user.role not in resettable
            or user.is_staff
            or user.is_superuser
            or requester.department_id is None
            or user.department_id != requester.department_id
            or (not is_executive and user.level != requester.level)
        ):
            return Response(
                {'error': 'forbidden', 'message': 'You can only reset students within your own remit.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.has_usable_password():
            return Response(
                {
                    'error': 'bad_request',
                    'message': 'That account has no password yet — the student should claim it first.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Only one live code per student: a new code retires the old one.
        PasswordResetCode.objects.filter(
            user=user, used_at__isnull=True
        ).update(used_at=timezone.now())
        code = PasswordResetCode.objects.create(
            user=user, code=_new_code(8), created_by=request.user
        )
        return Response({
            'matric_number': user.matric_number,
            'code': code.code,
            'expires_in_minutes': PasswordResetCode.RESET_TTL_MINUTES,
        })


class ResetPasswordView(APIView):
    """POST /api/auth/reset-password/ — student sets a new password using the
    one-time code they were given. Public + throttled like login."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, AccountThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        matric = (request.data.get('matric_number') or '').strip()
        code_value = (request.data.get('code') or '').strip()
        password = request.data.get('new_password') or ''
        if not (matric and code_value and password):
            return Response(
                {
                    'error': 'bad_request',
                    'message': 'matric_number, code and new_password are required.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        reset_code = (
            PasswordResetCode.objects
            .select_related('user')
            .filter(code__iexact=code_value, used_at__isnull=True)
            .first()
        )
        if (
            reset_code is None
            or (reset_code.user.matric_number or '').casefold() != matric.casefold()
        ):
            return Response(
                {'error': 'invalid_code', 'message': 'Invalid or expired reset code.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if reset_code.is_expired():
            return Response(
                {
                    'error': 'code_expired',
                    'message': 'That reset code has expired. Ask for a new one.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(password, user=reset_code.user)
        except Exception as exc:
            return Response(
                {'error': 'weak_password', 'message': ' '.join(exc.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reset_code.user.set_password(password)
        reset_code.user.save()
        # A reset must evict whoever held the old credentials: drop the token.
        Token.objects.filter(user=reset_code.user).delete()
        reset_code.used_at = timezone.now()
        reset_code.save(update_fields=['used_at'])
        # Retire any other live codes for this student.
        PasswordResetCode.objects.filter(
            user=reset_code.user, used_at__isnull=True
        ).update(used_at=timezone.now())
        return Response({'message': 'Password updated. You can now log in.'})


class ClaimBatchListView(APIView):
    """GET /api/auth/claim-batches/ — admin view of the shared claim codes."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        batches = ClaimBatch.objects.all()[:50]
        return Response([
            {
                'id': b.id,
                'code': b.code,
                'is_active': b.is_active,
                'created_at': b.created_at,
            }
            for b in batches
        ])


class DeactivateClaimBatchView(APIView):
    """POST /api/auth/claim-batches/{id}/deactivate/ — retire a claim code
    (e.g. once the claim window closes) so it can no longer be used."""

    permission_classes = [IsAdminUser]

    def post(self, request, pk):
        batch = ClaimBatch.objects.filter(pk=pk).first()
        if batch is None:
            return Response(
                {'error': 'not_found', 'message': 'No such claim batch.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        batch.is_active = False
        batch.save(update_fields=['is_active'])
        return Response({'id': batch.id, 'code': batch.code, 'is_active': batch.is_active})


class SetUserRoleView(APIView):
    """
    POST /api/auth/users/{id}/set-role/ — admin-only. Reps are normal
    students until an admin promotes them (owner decision): promotion grants
    the mark-paid power, so it never happens through self-service.
    """

    permission_classes = [IsAdminUser]

    def post(self, request, pk):
        role = request.data.get('role')
        if role not in (User.ROLE_STUDENT, *User.OFFICES):
            return Response(
                {
                    'error': 'bad_request',
                    'message': "role must be 'student', 'class_rep' or 'executive'.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = User.objects.filter(pk=pk).first()
        if user is None:
            return Response(
                {'error': 'not_found', 'message': 'No such user.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if user.id == request.user.id:
            return Response(
                {'error': 'forbidden', 'message': 'You cannot change your own role.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if user.role == User.ROLE_ADMIN or user.is_staff or user.is_superuser:
            return Response(
                {'error': 'forbidden', 'message': "An admin's role cannot be changed here."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Scope guard: a class rep can only promote students inside their own
        # department AND level, so reps stay scoped to the class they actually
        # represent. A cross-department or cross-level promotion by a rep is an
        # honest 403. Admins/staff/superusers are never restricted.
        if request.user.role == User.ROLE_CLASS_REP:
            if user.department_id != request.user.department_id:
                return Response(
                    {'error': 'forbidden', 'message': 'You can only manage users in your own department.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if (
                user.level
                and request.user.level
                and user.level != request.user.level
            ):
                return Response(
                    {'error': 'forbidden', 'message': 'You can only manage users in your own level.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        if role in User.OFFICES and (not user.department_id or user.graduated_at):
            return Response(
                {
                    'error': 'bad_request',
                    'message': 'An office holder must be a current student with a department.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            record_role_change(user, role, request.user, 'appointed')
        return Response({
            'user': {
                'id': user.id,
                'username': user.username,
                'role': user.role,
                'department': user.department.name if user.department_id else None,
                'level': user.level,
            }
        })


class UserListView(APIView):
    """
    GET /api/auth/users/?search=<matric|username|name> — admin-only lookup.

    Used by the role-management screen to resolve a matric number (or a name)
    to a user id before calling set-role. Returns only safe, non-sensitive
    fields and is capped so a broad search can't dump the whole user table.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        query = (
            request.query_params.get('search')
            or request.query_params.get('matric_number')
            or ''
        ).strip()
        qs = User.objects.all()
        if query:
            qs = qs.filter(
                Q(matric_number__iexact=query)
                | Q(username__iexact=query)
                | Q(matric_number__icontains=query)
                | Q(username__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
            )
        qs = qs.select_related('department').order_by('id')[:20]
        return Response([
            {
                'id': user.id,
                'username': user.username,
                'full_name': user.get_full_name() or user.username,
                'matric_number': user.matric_number,
                'role': user.role,
                'department': user.department.name if user.department_id else None,
            }
            for user in qs
        ])


class HandoverView(APIView):
    """
    POST /api/auth/handover/ — a class rep or executive passes their office
    to another student (`matric_number`), confirming with their own
    `password`. The caller becomes an ordinary student in the same step.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        # Handing over control of money is confirmed with the password, so a
        # borrowed phone or a stolen token is not enough to give it away.
        if not request.user.check_password(str(request.data.get('password') or '')):
            return Response(
                {'error': 'forbidden', 'message': 'Password is incorrect.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        matric = str(request.data.get('matric_number') or '').strip()
        successor = (
            User.objects.filter(matric_number__iexact=matric).first() if matric else None
        )
        office = request.user.role
        try:
            hand_over(request.user, successor)
        except LifecycleError as exc:
            return Response(
                {'error': 'bad_request', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            'message': 'Handover complete. You are now an ordinary student.',
            'office': office,
            'successor': {
                'matric_number': successor.matric_number,
                'full_name': successor.get_full_name() or successor.username,
            },
        })


class SessionView(APIView):
    """
    GET  /api/auth/sessions/ — admin: past session rollovers.
    POST /api/auth/sessions/ — admin: start a new session (`name`, optional
    `dry_run`). Moves every class up a level, graduates the final-year class
    and closes open fees. A session name can only ever be started once.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response([
            {
                'name': session.name,
                'started_at': session.started_at,
                'promoted': session.promoted,
                'graduated': session.graduated,
                'fees_closed': session.fees_closed,
            }
            for session in AcademicSession.objects.all()[:50]
        ])

    def post(self, request):
        dry_run = str(request.data.get('dry_run', '')).lower() in ('1', 'true', 'yes')
        try:
            result = start_session(
                str(request.data.get('name') or ''), request.user, dry_run=dry_run
            )
        except LifecycleError as exc:
            return Response(
                {'error': 'bad_request', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            result,
            status=status.HTTP_200_OK if dry_run else status.HTTP_201_CREATED,
        )
