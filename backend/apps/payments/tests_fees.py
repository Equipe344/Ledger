"""
Passing Paystack's fee to the student: the maths, and the payment flow.

The rule under test: when pass-through is on, the student is charged enough
that the department still receives the FULL fee after Paystack's cut — never
a kobo less — and only that exact charge settles the payment.
"""
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.contributions.models import Contribution
from apps.users.models import Department
from .fees import gateway_fee_kobo, online_charge
from .models import Payment

User = get_user_model()
PASS_ON = override_settings(PAYSTACK_PASS_FEE_TO_STUDENT=True)


class OnlineChargeMathsTests(SimpleTestCase):

    def test_off_by_default_the_charge_is_just_the_fee(self):
        self.assertEqual(online_charge(Decimal('5000.00')), Decimal('5000.00'))

    @PASS_ON
    def test_known_amounts(self):
        cases = {
            '1000.00': '1015.23',       # under the threshold: 1.5% only
            '5000.00': '5177.67',       # 1.5% + NGN 100, grossed up
            '200000.00': '202000.00',   # fee capped at NGN 2,000
        }
        for fee, charge in cases.items():
            self.assertEqual(online_charge(Decimal(fee)), Decimal(charge), fee)

    @PASS_ON
    def test_department_always_nets_the_full_fee_and_student_is_not_overcharged(self):
        # Every naira amount up to 3,000 (covers the NGN 2,500 threshold), then
        # a sweep up past the point where the cap takes over.
        fees = list(range(1, 3001)) + list(range(3000, 300001, 137))
        for naira in fees:
            fee_kobo = naira * 100
            charge_kobo = int(online_charge(Decimal(naira)) * 100)
            net = charge_kobo - gateway_fee_kobo(charge_kobo)
            self.assertGreaterEqual(net, fee_kobo, f'department short-changed at NGN {naira}')
            self.assertLessEqual(net - fee_kobo, 5, f'student overcharged at NGN {naira}')


@PASS_ON
class PassThroughFlowTests(APITestCase):

    def setUp(self):
        department = Department.objects.create(name='Physics', faculty='Science')
        admin = User.objects.create_user(
            username='boss', email='boss@example.com', password='S7rong!Passw0rd',
            matric_number='PHY/000', department=department, role='admin',
        )
        self.student = User.objects.create_user(
            username='student', email='student@example.com', password='S7rong!Passw0rd',
            matric_number='PHY/001', department=department, level='300',
        )
        self.fee = Contribution.objects.create(
            department=department, created_by=admin, title='Dues',
            amount=Decimal('5000.00'),
            deadline=timezone.now() + timezone.timedelta(days=30),
        )
        token = Token.objects.create(user=self.student)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _initiate(self, reference='REF-FEE'):
        with patch('apps.payments.views.requests.post') as mock_post:
            mock_post.return_value.json.return_value = {
                'status': True,
                'data': {'reference': reference, 'authorization_url': 'https://checkout.test/x'},
            }
            response = self.client.post('/api/payments/initiate/', {'contribution_id': self.fee.id})
        return response, mock_post

    def _webhook(self, reference, kobo):
        body = json.dumps({
            'event': 'charge.success',
            'data': {'reference': reference, 'amount': kobo, 'currency': 'NGN'},
        }).encode()
        signature = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512
        ).hexdigest()
        self.client.credentials()
        return self.client.post(
            '/api/payments/webhook/', body, content_type='application/json',
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

    def test_student_sees_the_online_total_before_paying(self):
        row = self.client.get('/api/contributions/').data[0]
        self.assertEqual((row['amount'], row['online_amount']), ('5000.00', '5177.67'))

    def test_gateway_is_asked_for_fee_plus_charge_but_the_fee_is_what_is_owed(self):
        response, mock_post = self._initiate()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.call_args.kwargs['json']['amount'], 517767)
        payment = Payment.objects.get(reference='REF-FEE')
        self.assertEqual(payment.amount, Decimal('5000.00'))
        self.assertEqual(payment.charge_amount, Decimal('5177.67'))

    def test_paying_the_full_charge_settles_and_counts_the_fee_as_collected(self):
        self._initiate()
        self._webhook('REF-FEE', 517767)
        payment = Payment.objects.get(reference='REF-FEE')
        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(payment.paid_amount, Decimal('5177.67'))
        # Totals count what the department is owed, not Paystack's cut.
        self.assertEqual(self.fee.total_collected(), Decimal('5000.00'))

    def test_paying_only_the_bare_fee_is_not_enough(self):
        self._initiate()
        self._webhook('REF-FEE', 500000)
        payment = Payment.objects.get(reference='REF-FEE')
        self.assertEqual(payment.status, Payment.STATUS_FAILED)
        self.assertEqual(payment.refund_status, Payment.REFUND_PENDING_REVIEW)

    def test_offline_payments_are_the_fee_only(self):
        response = self.client.post(
            '/api/payments/submit/',
            {'contribution_id': self.fee.id, 'channel': 'cash'}, format='multipart',
        )
        self.assertEqual(response.status_code, 201)
        payment = Payment.objects.get(method=Payment.METHOD_MANUAL)
        self.assertEqual((payment.amount, payment.charge_amount), (Decimal('5000.00'), None))
