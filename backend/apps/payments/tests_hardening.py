"""
Regression locks for the production-readiness pass.

Each test pins one money or access rule that did not hold before:
confirmed payments are write-once, nobody approves their own payment, proofs
are private, financial rows cannot be deleted, and a fee's price is frozen
once someone has paid it.
"""
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.contributions.models import Contribution
from apps.users.models import Department
from .models import Payment

User = get_user_model()
PASSWORD = 'S7rong!Passw0rd'


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HardeningTests(APITestCase):

    def setUp(self):
        self.department = Department.objects.create(name='Physics', faculty='Science')
        self.student = self._user('student', 'PHY/001')
        self.other_student = self._user('other', 'PHY/002')
        self.rep = self._user('rep', 'PHY/003', role='class_rep')
        self.other_rep = self._user('rep2', 'PHY/004', role='class_rep')
        self.admin = self._user('boss', 'PHY/005', role='admin')
        self.fee = Contribution.objects.create(
            department=self.department, created_by=self.admin, title='Dues',
            amount=Decimal('5000.00'),
            deadline=timezone.now() + timezone.timedelta(days=30),
        )

    def _user(self, name, matric, role='student', level='300'):
        return User.objects.create_user(
            username=name, email=f'{name}@example.com', password=PASSWORD,
            matric_number=matric, department=self.department, level=level, role=role,
        )

    def _as(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _online(self, reference, status=Payment.STATUS_PENDING, student=None):
        return Payment.objects.create(
            student=student or self.student, contribution=self.fee,
            payment_type=Payment.PAYMENT_CONTRIBUTION, amount=self.fee.amount,
            reference=reference, status=status, method=Payment.METHOD_ONLINE,
        )

    def _submit(self, user, name='proof.png', content=b'image-bytes'):
        self._as(user)
        return self.client.post(
            '/api/payments/submit/',
            {
                'contribution_id': self.fee.id,
                'channel': 'bank_transfer',
                'proof': SimpleUploadedFile(name, content),
            },
            format='multipart',
        )

    # --- confirmed payments are write-once ---------------------------------

    def test_verify_never_asks_the_gateway_about_a_settled_payment(self):
        self._online('REF-DONE', status=Payment.STATUS_SUCCESS)
        self._as(self.student)
        with patch('apps.payments.views.requests.get') as mock_get:
            # Even if the gateway would now say "reversed", nothing changes.
            mock_get.return_value.json.return_value = {
                'status': True, 'data': {'status': 'reversed'},
            }
            response = self.client.get('/api/payments/verify/REF-DONE/')
        self.assertEqual(response.status_code, 200)
        mock_get.assert_not_called()
        self.assertEqual(
            Payment.objects.get(reference='REF-DONE').status, Payment.STATUS_SUCCESS
        )

    def test_right_amount_in_the_wrong_currency_is_not_credited(self):
        payment = self._online('REF-USD')
        self._as(self.student)
        with patch('apps.payments.views.requests.get') as mock_get:
            mock_get.return_value.json.return_value = {
                'status': True,
                'data': {'status': 'success', 'amount': 500000, 'currency': 'USD'},
            }
            self.client.get('/api/payments/verify/REF-USD/')
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.STATUS_FAILED)
        self.assertEqual(payment.refund_status, Payment.REFUND_PENDING_REVIEW)

    def test_online_payment_needs_an_email(self):
        self.student.email = ''
        self.student.save()
        self._as(self.student)
        with patch('apps.payments.views.requests.post') as mock_post:
            response = self.client.post(
                '/api/payments/initiate/', {'contribution_id': self.fee.id}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'email_required')
        mock_post.assert_not_called()

    # --- offline review: nobody approves their own money --------------------

    def test_rep_cannot_approve_their_own_offline_payment(self):
        payment_id = self._submit(self.rep).data['payment']['id']
        queue = self.client.get('/api/payments/pending/')
        self.assertEqual(queue.data['count'], 0)

        response = self.client.post(
            f'/api/payments/{payment_id}/review/', {'action': 'approve'}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            Payment.objects.get(pk=payment_id).status, Payment.STATUS_PENDING
        )

    def test_another_rep_can_approve_it_and_is_recorded(self):
        payment_id = self._submit(self.rep).data['payment']['id']
        self._as(self.other_rep)
        response = self.client.post(
            f'/api/payments/{payment_id}/review/', {'action': 'approve'}
        )
        self.assertEqual(response.status_code, 200)
        payment = Payment.objects.get(pk=payment_id)
        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(payment.recorded_by, self.other_rep)

    def test_rep_without_a_department_sees_no_queue(self):
        self._submit(self.student)
        self.other_rep.department = None
        self.other_rep.save()
        self._as(self.other_rep)
        self.assertEqual(self.client.get('/api/payments/pending/').data['count'], 0)

    # --- proof uploads -------------------------------------------------------

    def test_proof_must_be_an_image_or_pdf_and_small(self):
        self.assertEqual(self._submit(self.student, name='run.exe').status_code, 400)
        too_big = b'x' * (5 * 1024 * 1024 + 1)
        self.assertEqual(
            self._submit(self.student, content=too_big).status_code, 400
        )
        self.assertFalse(Payment.objects.exists())

    def test_proof_is_private_to_its_owner_and_reviewers(self):
        submitted = self._submit(self.student).data['payment']
        url = f"/api/payments/{submitted['id']}/proof/"
        self.assertTrue(submitted['proof_url'].endswith(url))

        self.assertEqual(self.client.get(url).status_code, 200)   # the owner
        self._as(self.rep)
        self.assertEqual(self.client.get(url).status_code, 200)   # their rep
        self._as(self.other_student)
        self.assertEqual(self.client.get(url).status_code, 404)   # a classmate
        self.client.credentials()
        self.assertEqual(self.client.get(url).status_code, 401)   # the public

    # --- financial rows cannot disappear ------------------------------------

    def test_a_student_or_fee_with_payments_cannot_be_deleted(self):
        self._online('REF-KEEP', status=Payment.STATUS_SUCCESS)
        with self.assertRaises(ProtectedError):
            self.student.delete()
        with self.assertRaises(ProtectedError):
            self.fee.delete()
        with self.assertRaises(ProtectedError):
            self.department.delete()

    def test_fee_price_is_frozen_once_someone_has_paid(self):
        self._as(self.admin)
        url = f'/api/contributions/{self.fee.id}/'
        self.assertEqual(self.client.patch(url, {'amount': '4000.00'}).status_code, 200)

        self._online('REF-PAID', status=Payment.STATUS_SUCCESS)
        self.assertEqual(self.client.patch(url, {'amount': '50.00'}).status_code, 400)
        self.assertEqual(self.client.patch(url, {'title': 'Annual dues'}).status_code, 200)
        self.fee.refresh_from_db()
        self.assertEqual(self.fee.amount, Decimal('4000.00'))

    # --- account takeover paths ---------------------------------------------

    def test_rep_can_reset_a_student_but_not_another_rep(self):
        self._as(self.rep)
        ok = self.client.post('/api/auth/reset-code/', {'matric_number': 'PHY/001'})
        self.assertEqual(ok.status_code, 200)
        blocked = self.client.post('/api/auth/reset-code/', {'matric_number': 'PHY/004'})
        self.assertEqual(blocked.status_code, 403)

    def test_password_reset_revokes_the_old_token(self):
        old_token = Token.objects.create(user=self.student)
        self._as(self.admin)
        code = self.client.post(
            '/api/auth/reset-code/', {'matric_number': 'PHY/001'}
        ).data['code']
        self.client.credentials()
        response = self.client.post('/api/auth/reset-password/', {
            'matric_number': 'PHY/001', 'code': code, 'new_password': 'N3w!Passw0rd-x',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Token.objects.filter(key=old_token.key).exists())

    def test_an_admin_cannot_be_demoted_through_set_role(self):
        second_admin = self._user('boss2', 'PHY/006', role='admin')
        self._as(self.admin)
        response = self.client.post(
            f'/api/auth/users/{second_admin.id}/set-role/', {'role': 'student'}
        )
        self.assertEqual(response.status_code, 403)

    def test_rep_cannot_move_themselves_to_another_level(self):
        self._as(self.rep)
        response = self.client.patch('/api/auth/me/', {'level': '100'})
        self.assertEqual(response.status_code, 400)
        self.rep.refresh_from_db()
        self.assertEqual(self.rep.level, '300')

    # --- treasurer export ----------------------------------------------------

    def _export(self, user, fee=None):
        self._as(user)
        return self.client.get(f'/api/contributions/{(fee or self.fee).id}/export/')

    def test_export_lists_who_paid_and_who_has_not(self):
        Payment.objects.create(
            student=self.student, contribution=self.fee, amount=self.fee.amount,
            payment_type=Payment.PAYMENT_CONTRIBUTION, reference='MANUAL-1',
            status=Payment.STATUS_SUCCESS, method=Payment.METHOD_MANUAL,
            receipt_reference='TELLER-77', recorded_by=self.rep,
        )
        response = self._export(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="fee-', response['Content-Disposition'])
        text = response.content.decode('utf-8-sig')
        lines = text.splitlines()
        self.assertIn('Paid,1', lines)
        self.assertIn('Outstanding,3', lines)          # other_student, rep, rep2
        self.assertIn('Collected (NGN),5000.00', lines)
        paid_row = next(line for line in lines if line.startswith('PHY/001,'))
        self.assertIn('Paid,Offline,', paid_row)
        self.assertIn('MANUAL-1,TELLER-77,', paid_row)
        unpaid_row = next(line for line in lines if line.startswith('PHY/002,'))
        self.assertIn(',Not paid,', unpaid_row)
        self.assertNotIn('PHY/005', text)              # the admin owes nothing

    def test_export_neutralises_spreadsheet_formulas_in_names(self):
        self.other_student.first_name = '=HYPERLINK("http://evil.test")'
        self.other_student.save()
        text = self._export(self.admin).content.decode('utf-8-sig')
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(',=HYPERLINK', text)

    def test_a_student_cannot_export_or_read_the_roster(self):
        self.assertEqual(self._export(self.student).status_code, 403)

    def test_a_rep_cannot_read_or_export_another_levels_roster(self):
        other_level_fee = Contribution.objects.create(
            department=self.department, created_by=self.admin, title='100L dues',
            amount=Decimal('1000.00'), target_level='100',
            deadline=timezone.now() + timezone.timedelta(days=30),
        )
        self.assertEqual(self._export(self.rep, other_level_fee).status_code, 403)
        self.assertEqual(
            self.client.get(f'/api/contributions/{other_level_fee.id}/payments/').status_code,
            403,
        )
