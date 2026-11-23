"""
Who holds office, how it changes hands, and what a new session does.

Covers the executive role, handover, the session rollover (promotion,
graduation, fee closing), plus the account-safety rules added alongside:
claim codes bound to their import, the registration switch, import limits
and the per-account rate limit.
"""
from decimal import Decimal
from datetime import timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.contributions.models import Contribution
from apps.payments.models import Payment
from .models import AcademicSession, ClaimBatch, Department, RoleChange, User

PASSWORD = 'S7rong!Passw0rd'


class LifecycleBase(APITestCase):

    def setUp(self):
        cache.clear()  # throttle counters
        self.department = Department.objects.create(
            name='Physics', faculty='Science', final_level='400'
        )
        self.admin = self._user('boss', 'PHY/000', role=User.ROLE_ADMIN)
        self.executive = self._user('finsec', 'PHY/301', role=User.ROLE_EXECUTIVE)
        self.rep = self._user('rep', 'PHY/302', role=User.ROLE_CLASS_REP)
        self.student = self._user('student', 'PHY/303')
        self.fresher = self._user('fresher', 'PHY/101', level='100')
        self.finalist = self._user('finalist', 'PHY/401', level='400')

    def _user(self, name, matric, role=User.ROLE_STUDENT, level='300', department='own'):
        return User.objects.create_user(
            username=name, email=f'{name}@example.com', password=PASSWORD,
            matric_number=matric, role=role, level=level,
            department=self.department if department == 'own' else department,
        )

    def _as(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _fee(self, level=None, title='Dues'):
        return Contribution.objects.create(
            department=self.department, created_by=self.admin, title=title,
            amount=Decimal('5000.00'), target_level=level,
            deadline=timezone.now() + timedelta(days=30),
        )


class ExecutiveRoleTests(LifecycleBase):

    def test_admin_appoints_an_executive_and_it_is_audited(self):
        self._as(self.admin)
        response = self.client.post(
            f'/api/auth/users/{self.student.id}/set-role/', {'role': 'executive'}
        )
        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.role, User.ROLE_EXECUTIVE)
        change = RoleChange.objects.get(user=self.student)
        self.assertEqual(
            (change.old_role, change.new_role, change.changed_by, change.reason),
            ('student', 'executive', self.admin, 'appointed'),
        )

    def test_executive_manages_every_level_but_a_rep_only_their_own(self):
        fee_100 = self._fee(level='100')
        payload = {
            'title': 'Freshers week', 'amount': '1000.00', 'target_level': '100',
            'deadline': (timezone.now() + timedelta(days=5)).isoformat(),
        }
        self._as(self.executive)
        self.assertEqual(self.client.post('/api/contributions/', payload).status_code, 201)
        self.assertEqual(
            self.client.get(f'/api/contributions/{fee_100.id}/payments/').status_code, 200
        )
        self._as(self.rep)
        self.assertEqual(self.client.post('/api/contributions/', payload).status_code, 400)

    def test_executive_reviews_other_levels_but_never_their_own_payment(self):
        fee = self._fee()
        def pending(student):
            return Payment.objects.create(
                student=student, contribution=fee, amount=fee.amount,
                payment_type=Payment.PAYMENT_CONTRIBUTION,
                reference=f'SELF-{student.id}', method=Payment.METHOD_MANUAL,
            )
        theirs, own = pending(self.fresher), pending(self.executive)

        self._as(self.executive)
        queue = self.client.get('/api/payments/pending/').data
        self.assertEqual([row['id'] for row in queue['results']], [theirs.id])
        approve = {'action': 'approve'}
        self.assertEqual(
            self.client.post(f'/api/payments/{theirs.id}/review/', approve).status_code, 200
        )
        self.assertEqual(
            self.client.post(f'/api/payments/{own.id}/review/', approve).status_code, 403
        )

    def test_executive_still_owes_dues(self):
        self.assertIn(self.executive, self._fee().eligible_students())


class HandoverTests(LifecycleBase):

    def _hand_over(self, holder, matric, password=PASSWORD):
        self._as(holder)
        return self.client.post(
            '/api/auth/handover/', {'matric_number': matric, 'password': password}
        )

    def test_executive_hands_over_and_loses_the_office_in_the_same_step(self):
        response = self._hand_over(self.executive, 'PHY/101')
        self.assertEqual(response.status_code, 200)
        self.executive.refresh_from_db()
        self.fresher.refresh_from_db()
        self.assertEqual(self.executive.role, User.ROLE_STUDENT)
        self.assertEqual(self.fresher.role, User.ROLE_EXECUTIVE)
        self.assertEqual(RoleChange.objects.filter(reason='handover').count(), 2)
        # The old holder's token no longer opens management endpoints.
        self.assertEqual(self.client.get('/api/payments/pending/').status_code, 403)

    def test_handover_needs_the_holders_password(self):
        response = self._hand_over(self.executive, 'PHY/101', password='wrong')
        self.assertEqual(response.status_code, 403)
        self.executive.refresh_from_db()
        self.assertEqual(self.executive.role, User.ROLE_EXECUTIVE)

    def test_rep_can_only_hand_over_inside_their_own_class(self):
        self.assertEqual(self._hand_over(self.rep, 'PHY/101').status_code, 400)
        self.assertEqual(self._hand_over(self.rep, 'PHY/303').status_code, 200)

    def test_cannot_hand_over_to_an_unclaimed_or_outside_account(self):
        unclaimed = self._user('ghost', 'PHY/305')
        unclaimed.set_unusable_password()
        unclaimed.save()
        other = Department.objects.create(name='Chemistry', faculty='Science')
        self._user('outsider', 'CHM/301', department=other)
        for matric in ('PHY/305', 'CHM/301', 'PHY/302', 'NOPE'):
            self.assertEqual(self._hand_over(self.executive, matric).status_code, 400, matric)
        self.assertFalse(RoleChange.objects.exists())

    def test_guessing_the_handover_password_is_limited_per_account(self):
        statuses = [
            self._hand_over(self.executive, 'PHY/101', password='guess').status_code
            for _ in range(11)
        ]
        self.assertEqual(statuses[:10], [403] * 10)
        self.assertEqual(statuses[10], 429)
        self.executive.refresh_from_db()
        self.assertEqual(self.executive.role, User.ROLE_EXECUTIVE)

    def test_a_plain_student_has_nothing_to_hand_over(self):
        self.assertEqual(self._hand_over(self.student, 'PHY/101').status_code, 400)


class NewSessionTests(LifecycleBase):

    def _start(self, name='2026/2027', **extra):
        self._as(self.admin)
        return self.client.post('/api/auth/sessions/', {'name': name, **extra})

    def test_dry_run_reports_and_changes_nothing(self):
        self._fee()
        response = self._start(dry_run='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.data['promoted'], response.data['graduated'], response.data['fees_closed']),
            (4, 1, 1),
        )
        self.fresher.refresh_from_db()
        self.assertEqual(self.fresher.level, '100')
        self.assertFalse(AcademicSession.objects.exists())
        self.assertFalse(Contribution.objects.get().is_closed)

    def test_everyone_moves_up_one_level_and_the_final_year_graduates(self):
        self.assertEqual(self._start().status_code, 201)
        for user in (self.fresher, self.student, self.rep, self.finalist, self.admin):
            user.refresh_from_db()
        self.assertEqual(self.fresher.level, '200')
        self.assertEqual(self.student.level, '400')   # promoted, NOT also graduated
        self.assertIsNone(self.student.graduated_at)
        self.assertIsNotNone(self.finalist.graduated_at)
        self.assertEqual(self.finalist.level, '400')
        self.assertEqual(self.admin.level, '300')     # admins are not students
        # A class rep moves up with their class and keeps the office.
        self.assertEqual((self.rep.level, self.rep.role), ('400', User.ROLE_CLASS_REP))

    def test_a_graduating_office_holder_loses_the_office(self):
        self.executive.level = '400'
        self.executive.save()
        self._start()
        self.executive.refresh_from_db()
        self.assertEqual(self.executive.role, User.ROLE_STUDENT)
        self.assertTrue(RoleChange.objects.filter(user=self.executive, reason='graduated').exists())

    def test_last_sessions_fees_are_closed_so_the_new_class_is_not_billed(self):
        fee = self._fee(level='100')
        self._start()
        fee.refresh_from_db()
        self.assertTrue(fee.is_closed)

    def test_a_session_can_only_be_started_once(self):
        self._start()
        self.assertEqual(self._start().status_code, 400)
        self.fresher.refresh_from_db()
        self.assertEqual(self.fresher.level, '200')   # not promoted twice
        self.assertEqual(AcademicSession.objects.count(), 1)

    def test_only_an_admin_can_start_a_session(self):
        self._as(self.executive)
        self.assertEqual(
            self.client.post('/api/auth/sessions/', {'name': '2026/2027'}).status_code, 403
        )

    def test_a_graduate_keeps_their_history_but_is_no_longer_billed(self):
        old_fee = self._fee(title='Final year dues')
        Payment.objects.create(
            student=self.finalist, contribution=old_fee, amount=old_fee.amount,
            payment_type=Payment.PAYMENT_CONTRIBUTION, reference='REF-OLD',
            status=Payment.STATUS_SUCCESS,
        )
        self._start()
        new_fee = self._fee(title='New session dues')

        self._as(self.finalist)
        self.assertEqual(len(self.client.get('/api/payments/history/').data), 1)
        self.assertEqual(len(self.client.get('/api/contributions/').data), 0)
        self.assertEqual(
            self.client.post('/api/payments/initiate/', {'contribution_id': new_fee.id}).status_code,
            404,
        )
        self.finalist.refresh_from_db()
        self.assertNotIn(self.finalist, new_fee.eligible_students())


class CarriedDebtTests(LifecycleBase):
    """Unpaid mandatory dues follow the student into every later session."""

    def setUp(self):
        super().setUp()
        self.dues = self._fee(level='100', title='100L dues')   # fresher owes it
        self.shirt = Contribution.objects.create(
            department=self.department, created_by=self.admin, title='Shirt',
            amount=Decimal('3000.00'), is_mandatory=False,
            deadline=timezone.now() + timedelta(days=30),
        )
        self.newcomer_dues = None

    def _start(self, name='2026/2027'):
        self._as(self.admin)
        return self.client.post('/api/auth/sessions/', {'name': name})

    def _pay(self, student, fee, reference):
        return Payment.objects.create(
            student=student, contribution=fee, amount=fee.amount,
            payment_type=Payment.PAYMENT_CONTRIBUTION, reference=reference,
            status=Payment.STATUS_SUCCESS,
        )

    def _titles(self, user):
        self._as(user)
        return sorted(row['title'] for row in self.client.get('/api/contributions/').data)

    def test_unpaid_mandatory_dues_follow_the_student_up_a_level(self):
        response = self._start()
        self.assertEqual(response.data['debts_carried'], 1)   # the optional shirt is not a debt
        self.fresher.refresh_from_db()
        self.assertEqual(self.fresher.level, '200')
        # Still owed, still visible, flagged as carried over — and payable.
        self._as(self.fresher)
        rows = self.client.get('/api/contributions/').data
        self.assertEqual(
            [(r['title'], r['carried_over'], r['has_paid']) for r in rows],
            [('100L dues', True, False)],
        )
        submit = self.client.post(
            '/api/payments/submit/',
            {'contribution_id': self.dues.id, 'channel': 'cash'},
            format='multipart',
        )
        self.assertEqual(submit.status_code, 201)

    def test_a_student_who_paid_carries_no_debt(self):
        self._pay(self.fresher, self.dues, 'REF-PAID')
        self.assertEqual(self._start().data['debts_carried'], 0)
        self.assertEqual(self._titles(self.fresher), [])

    def test_the_new_class_is_not_billed_for_last_sessions_fee(self):
        self._start()
        newcomer = self._user('newcomer', 'PHY/111', level='100')
        self.assertEqual(self._titles(newcomer), [])
        self._as(newcomer)
        self.assertEqual(
            self.client.post('/api/payments/initiate/', {'contribution_id': self.dues.id}).status_code,
            404,
        )

    def test_debt_survives_a_second_session_and_graduation(self):
        final_dues = self._fee(level='400', title='Final dues')   # finalist owes it
        self._start('2026/2027')
        self._start('2027/2028')
        self.finalist.refresh_from_db()
        self.assertIsNotNone(self.finalist.graduated_at)
        self.assertEqual(self._titles(self.finalist), ['Final dues'])
        # Recorded once, not once per session.
        self.assertEqual(final_dues.debts.filter(student=self.finalist).count(), 1)

    def test_roster_of_a_past_fee_still_shows_who_owed_and_who_paid(self):
        payer = self._user('payer', 'PHY/102', level='100')
        self._pay(payer, self.dues, 'REF-PAYER')
        self._start()
        self.dues.refresh_from_db()
        self.assertEqual(set(self.dues.eligible_students()), {self.fresher, payer})
        self.assertEqual(self.dues.outstanding_count(), 1)
        self.assertEqual(self.dues.total_expected(), Decimal('10000.00'))

    def test_settling_a_carried_debt_offline_and_the_guards_around_it(self):
        self._start()
        self._as(self.executive)
        url = f'/api/contributions/{self.dues.id}/payments/'
        # Someone who never owed it cannot be marked paid on it.
        wrong = self.client.post(url, {'matric_number': 'PHY/303', 'receipt_reference': 'T-1'})
        self.assertEqual(wrong.status_code, 400)
        right = self.client.post(url, {'matric_number': 'PHY/101', 'receipt_reference': 'T-2'})
        self.assertEqual(right.status_code, 201)
        self.assertEqual(self._titles(self.fresher), ['100L dues'])
        self._as(self.fresher)
        self.assertTrue(self.client.get('/api/contributions/').data[0]['has_paid'])

    def test_a_fee_closed_before_rollover_is_not_carried(self):
        self.dues.is_closed = True          # e.g. the event was cancelled
        self.dues.save()
        self.assertEqual(self._start().data['debts_carried'], 0)
        self.assertEqual(self._titles(self.fresher), [])

    def test_fees_from_before_the_previous_session_are_never_carried(self):
        self._start('2026/2027')            # carries the 100L dues
        stale = self._fee(title='Stale fee')
        Contribution.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(days=400)
        )
        current = self._fee(title='This year')
        response = self._start('2027/2028')
        carried = set(Contribution.objects.filter(carried_over=True).values_list('title', flat=True))
        self.assertIn('This year', carried)
        self.assertNotIn('Stale fee', carried)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(current.debts.exists())

    def test_an_office_holder_sees_their_own_dues_with_scope_mine(self):
        self.executive.level = '100'
        self.executive.save()
        self._start()                      # executive now owes the carried 100L dues
        self._as(self.executive)
        mine = self.client.get('/api/contributions/?scope=mine').data
        self.assertEqual([row['title'] for row in mine], ['100L dues'])
        self.assertTrue(mine[0]['carried_over'])

    def test_a_past_sessions_fee_cannot_be_reopened_or_edited(self):
        self._start()
        self._as(self.admin)
        response = self.client.patch(
            f'/api/contributions/{self.dues.id}/', {'is_closed': False}, format='json'
        )
        self.assertEqual(response.status_code, 400)


class AccountSafetyTests(LifecycleBase):

    def _import(self, rows):
        csv = 'first_name,last_name,matric_number\n' + '\n'.join(rows) + '\n'
        self._as(self.admin)
        return self.client.post(
            '/api/auth/import/',
            {'file': SimpleUploadedFile('roster.csv', csv.encode())},
            format='multipart',
        )

    def _claim(self, matric, first_name, code):
        self.client.credentials()
        return self.client.post('/api/auth/claim/', {
            'matric_number': matric, 'first_name': first_name,
            'batch_code': code, 'password': PASSWORD,
        })

    def test_a_claim_code_only_works_for_its_own_import(self):
        code_a = self._import(['Ada,Obi,PHY/900']).data['claim_batch_code']
        code_b = self._import(['Bola,Ade,PHY/901']).data['claim_batch_code']
        self.assertEqual(self._claim('PHY/900', 'Ada', code_b).status_code, 400)
        self.assertEqual(self._claim('PHY/900', 'Ada', code_a).status_code, 200)

    def test_re_importing_reissues_a_fresh_code_to_unclaimed_students(self):
        old_code = self._import(['Ada,Obi,PHY/900', 'Bola,Ade,PHY/901']).data['claim_batch_code']
        self.assertEqual(self._claim('PHY/901', 'Bola', old_code).status_code, 200)
        ClaimBatch.objects.filter(code=old_code).update(
            created_at=timezone.now() - timedelta(days=ClaimBatch.CLAIM_TTL_DAYS + 1)
        )
        again = self._import(['Ada,Obi,PHY/900', 'Bola,Ade,PHY/901'])
        self.assertEqual((again.data['created'], again.data['reissued']), (0, 1))
        new_code = again.data['claim_batch_code']
        self.assertEqual(self._claim('PHY/900', 'Ada', old_code).status_code, 400)
        self.assertEqual(self._claim('PHY/900', 'Ada', new_code).status_code, 200)
        # A student who already claimed is not handed back to a code.
        self.assertEqual(self._claim('PHY/901', 'Bola', new_code).status_code, 400)

    def test_a_claim_code_expires(self):
        code = self._import(['Ada,Obi,PHY/900']).data['claim_batch_code']
        ClaimBatch.objects.filter(code=code).update(
            created_at=timezone.now() - timedelta(days=ClaimBatch.CLAIM_TTL_DAYS + 1)
        )
        self.assertEqual(self._claim('PHY/900', 'Ada', code).status_code, 400)

    def test_an_import_that_creates_nobody_mints_no_code(self):
        response = self._import(['Dup,Licate,PHY/303'])   # already exists
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('claim_batch_code', response.data)
        self.assertFalse(ClaimBatch.objects.exists())

    def test_an_oversized_roster_is_refused(self):
        self._as(self.admin)
        big = SimpleUploadedFile('roster.csv', b'first_name,matric_number\n' + b'x' * 1_000_001)
        response = self.client.post('/api/auth/import/', {'file': big}, format='multipart')
        self.assertEqual(response.status_code, 400)

    @override_settings(ALLOW_SELF_REGISTRATION=False)
    def test_sign_up_can_be_closed(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'walkin', 'password': PASSWORD, 'email': 'walkin@example.com',
            'matric_number': 'PHY/999', 'first_name': 'Walk', 'last_name': 'In',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error'], 'registration_closed')
        self.assertFalse(User.objects.filter(username='walkin').exists())

    def test_guessing_one_account_is_limited_even_across_many_addresses(self):
        statuses = [
            self.client.post(
                '/api/auth/login/',
                {'matric_number': 'PHY/303', 'password': 'wrong-guess'},
                REMOTE_ADDR=f'10.0.0.{attempt}',
            ).status_code
            for attempt in range(1, 12)
        ]
        self.assertEqual(statuses[:10], [400] * 10)
        self.assertEqual(statuses[10], 429)

    def test_many_students_behind_one_address_are_not_locked_out(self):
        for number in range(30):
            response = self.client.post(
                '/api/auth/login/',
                {'matric_number': f'PHY/7{number:02d}', 'password': 'x'},
                REMOTE_ADDR='10.9.9.9',
            )
            self.assertEqual(response.status_code, 400)
