import hashlib
import hmac
import json
import logging
import os
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import FileResponse
from rest_framework import generics, permissions
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.contributions.models import Contribution
from apps.users.permissions import IsClassRepOrAdmin
from .models import Payment, Transaction
from .permissions import IsAdminUser, is_admin
from .serializers import (
    PaymentSerializer,
    PendingPaymentSerializer,
    UnverifiedPaymentSerializer,
)

logger = logging.getLogger(__name__)

PAYSTACK_API = 'https://api.paystack.co'
CURRENCY = 'NGN'

# Offline proof uploads: a phone screenshot or a bank PDF, nothing else.
PROOF_MAX_BYTES = 5 * 1024 * 1024
PROOF_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}


def _error(code, message, status):
    """The one error shape every endpoint uses (API contract §7)."""
    return Response({'error': code, 'message': message}, status=status)


def _gateway_unavailable():
    return _error(
        'gateway_unavailable',
        'Payment gateway is unavailable. Try again shortly.',
        502,
    )


def _paystack_data(response, action):
    """The `data` object of a successful Paystack reply, or None."""
    try:
        result = response.json()
    except ValueError:
        # Non-JSON body (e.g. a proxy's HTML error page).
        result = None
    if not isinstance(result, dict) or not result.get('status') or not result.get('data'):
        logger.warning('paystack %s rejected status_code=%s', action, response.status_code)
        return None
    return result['data']


def _as_kobo(value):
    """Gateway money (kobo) as an exact int, or None when unusable."""
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _flag_for_refund_review(payment):
    """
    Fail the row and queue it for a human. A flag is only set while it is still
    `none`, so a reviewer's decision ('refunded'/'rejected') is never overwritten.
    """
    payment.status = Payment.STATUS_FAILED
    if payment.refund_status == Payment.REFUND_NONE:
        payment.refund_status = Payment.REFUND_PENDING_REVIEW


def _settle_gateway_charge(payment, paid_kobo, currency=None):
    """
    Apply a gateway-reported charge to a Payment row (caller saves).

    One rule, used by BOTH the webhook and the verify endpoint so they can
    never disagree about the same charge:

      * exactly the agreed fee, in naira  -> success
      * more or less than the agreed fee  -> failed + refund review
      * an amount we cannot verify        -> failed + refund review (fail closed)
      * a currency other than NGN         -> failed + refund review
      * a fee this student already paid   -> failed + refund review (duplicate)

    A mismatch means the gateway really took the student's money for a fee we
    are not crediting, so it is never silently absorbed: the row fails,
    `paid_amount` records what was actually taken, and a human reviews it
    before any refund is issued.
    """
    expected_kobo = int(payment.amount * 100)
    actual_kobo = _as_kobo(paid_kobo)

    if actual_kobo is not None:
        # Kobo -> naira is exact at 2dp, so nothing is lost recording it.
        payment.paid_amount = (Decimal(actual_kobo) / 100).quantize(Decimal('0.01'))

    already_credited = (
        payment.contribution_id is not None
        and Payment.objects.filter(
            student_id=payment.student_id,
            contribution_id=payment.contribution_id,
            status=Payment.STATUS_SUCCESS,
        ).exclude(pk=payment.pk).exists()
    )
    wrong_currency = currency is not None and currency != CURRENCY

    if already_credited or wrong_currency or actual_kobo != expected_kobo:
        _flag_for_refund_review(payment)
    else:
        payment.status = Payment.STATUS_SUCCESS


def _save_settled(payment):
    """
    Persist a settled charge.

    If we LOSE a concurrent race — the DB's unique_success_per_student_fee
    constraint fires because another charge for this fee was credited a moment
    ago — the money was still taken, so the row fails into refund review
    instead of double-crediting. The inner atomic block is a savepoint, so the
    constraint error cannot poison an enclosing transaction.
    """
    try:
        with transaction.atomic():
            payment.save()
    except IntegrityError:
        _flag_for_refund_review(payment)
        payment.save()


def _settle_online_payment(payment_id, paid_kobo, currency):
    """
    Settle one online payment under a row lock; returns the fresh row.

    The lock serialises a webhook delivery racing the student's own verify
    call. A payment that is already `success` is returned untouched: a
    confirmed financial record is never rewritten by a later event.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.status != Payment.STATUS_SUCCESS:
            _settle_gateway_charge(payment, paid_kobo, currency)
            _save_settled(payment)
            logger.info(
                'payment %s settled status=%s refund_status=%s',
                payment.reference, payment.status, payment.refund_status,
            )
        return payment


def _open_contribution_for(user, contribution_id):
    """
    The fee this student may pay right now, or None.

    The same rule as the student fee list (`Contribution.payable_by`): this
    session's open fees for their class, plus dues carried over from earlier
    sessions. A stale id can never be used to pay into a closed collection.
    """
    return Contribution.payable_by(user).filter(id=contribution_id).first()


def _has_paid(user, contribution):
    return Payment.objects.filter(
        student=user, contribution=contribution, status=Payment.STATUS_SUCCESS
    ).exists()


def _contribution_or_error(request):
    """(contribution, None) for a fee the student may pay, else (None, Response)."""
    contribution_id = request.data.get('contribution_id')
    if not contribution_id:
        return None, _error('bad_request', 'contribution_id is required.', 400)
    try:
        contribution_id = int(contribution_id)
    except (TypeError, ValueError):
        return None, _error('bad_request', 'contribution_id must be a number.', 400)

    contribution = _open_contribution_for(request.user, contribution_id)
    if contribution is None:
        return None, _error(
            'not_found', 'Contribution not found or not available to you.', 404
        )
    if _has_paid(request.user, contribution):
        return None, _error(
            'already_paid', 'You have already paid for this contribution.', 409
        )
    return contribution, None


class PaymentListView(generics.ListAPIView):
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Payment.objects.filter(student=self.request.user).select_related(
            'contribution'
        )


class InitializePaymentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        contribution, error = _contribution_or_error(request)
        if error is not None:
            return error

        user = request.user
        if not user.email:
            # Paystack requires a customer email; say so instead of a vague 502.
            return _error(
                'email_required',
                'Add an email address to your account before paying online.',
                400,
            )

        # The amount ALWAYS comes from the contribution — the student never
        # types a price. Kobo conversion is exact (Decimal, never float).
        amount = contribution.amount

        try:
            response = requests.post(
                f'{PAYSTACK_API}/transaction/initialize',
                headers={
                    'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}',
                    'Content-Type': 'application/json',
                },
                json={
                    'email': user.email,
                    'amount': int(amount * 100),
                    'currency': CURRENCY,
                    # Returns the student to the SPA, which then calls verify.
                    # The webhook still settles the money server-side.
                    'callback_url': f'{settings.FRONTEND_URL}/payment/callback',
                    # Shown on the Paystack dashboard: ties a charge back to our
                    # records during reconciliation. Never trusted on the way in.
                    'metadata': {
                        'contribution_id': contribution.id,
                        'student_id': user.id,
                        'matric_number': user.matric_number or '',
                    },
                },
                timeout=10,
            )
        except requests.exceptions.RequestException:
            logger.warning('paystack initialize unavailable')
            return _gateway_unavailable()

        data = _paystack_data(response, 'initialize')
        if not data or not data.get('reference') or not data.get('authorization_url'):
            return _gateway_unavailable()

        payment = Payment.objects.create(
            student=user,
            contribution=contribution,
            payment_type=Payment.PAYMENT_CONTRIBUTION,
            amount=amount,
            reference=data['reference'],
            status=Payment.STATUS_PENDING,
            method=Payment.METHOD_ONLINE,
        )

        return Response({
            'message': 'Payment initialized successfully.',
            'reference': payment.reference,
            'checkout_url': data['authorization_url'],
            # Kept for backward compatibility with earlier integrations.
            'authorization_url': data['authorization_url'],
            'payment': PaymentSerializer(payment).data,
        })


class VerifyPaymentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, reference):
        payment = Payment.objects.filter(
            reference=reference, student=request.user
        ).first()
        if not payment:
            return _error('not_found', 'Payment not found.', 404)

        # Only a pending online charge has anything to learn from the gateway.
        # A settled row (or an offline one) is returned as it stands, so a
        # verify call can never rewrite a confirmed financial record.
        if (
            payment.method == Payment.METHOD_ONLINE
            and payment.status == Payment.STATUS_PENDING
        ):
            try:
                response = requests.get(
                    f'{PAYSTACK_API}/transaction/verify/{reference}',
                    headers={'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}'},
                    timeout=10,
                )
            except requests.exceptions.RequestException:
                logger.warning('paystack verify unavailable')
                return _gateway_unavailable()

            data = _paystack_data(response, 'verify')
            if data is None:
                return _gateway_unavailable()

            # Only a TERMINAL outcome may change our row. 'abandoned',
            # 'pending', 'ongoing' and 'processing' leave it untouched.
            gateway_status = data.get('status')
            if gateway_status == 'success':
                payment = _settle_online_payment(
                    payment.pk, data.get('amount'), data.get('currency')
                )
            elif gateway_status in ('failed', 'reversed'):
                # Declined or reversed: no money is being kept, nothing to
                # refund. Saved through the model so the student is notified.
                with transaction.atomic():
                    payment = Payment.objects.select_for_update().get(pk=payment.pk)
                    if payment.status == Payment.STATUS_PENDING:
                        payment.status = Payment.STATUS_FAILED
                        payment.save()

        return Response({
            'message': 'Payment verification completed.',
            'payment': PaymentSerializer(payment, context={'request': request}).data,
        })


class PaymentDetailView(generics.RetrieveAPIView):
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Payment.objects.filter(student=self.request.user)


class PaystackWebhookView(APIView):
    """
    POST /api/payments/webhook/ — Paystack's server-to-server callback.

    Authenticity is the HMAC-SHA512 of the RAW body, keyed with our secret key
    (`x-paystack-signature`). The body is verified before it is parsed.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []  # the signature is the authentication

    def post(self, request):
        payload = request.body
        signature = request.headers.get('x-paystack-signature')
        if not signature:
            return _error('bad_request', 'Missing signature.', 400)

        expected = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode(), payload, hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return _error('bad_request', 'Invalid signature.', 400)

        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            event = None
        if not isinstance(event, dict):
            return _error('bad_request', 'Invalid payload.', 400)

        charge = event.get('data')
        if event.get('event') == 'charge.success' and isinstance(charge, dict):
            reference = charge.get('reference')
            # Exact reference only — never "the latest pending payment".
            payment = Payment.objects.filter(
                reference=reference, method=Payment.METHOD_ONLINE
            ).first()

            # Audit proof: store exactly what the gateway said. The first
            # delivery wins; events for unknown references are kept too.
            if reference:
                Transaction.objects.get_or_create(
                    reference=reference,
                    defaults={'payment': payment, 'raw_payload': event},
                )
            logger.info(
                'paystack webhook event=charge.success reference=%s payment_found=%s',
                reference, payment is not None,
            )

            if payment:
                _settle_online_payment(
                    payment.pk, charge.get('amount'), charge.get('currency')
                )

        return Response({'received': True})


class UnverifiedPaymentsView(APIView):
    """
    GET /api/payments/unverified/ — admin-only refund-review queue.

    Every payment flagged `pending_review`: the gateway took money we could not
    credit. Read-only; the refund decision is recorded in the Django admin.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        flagged = (
            Payment.objects.filter(refund_status=Payment.REFUND_PENDING_REVIEW)
            .select_related('student', 'contribution')
            .order_by('-created_at')
        )
        results = UnverifiedPaymentSerializer(flagged, many=True).data
        return Response({'count': len(results), 'results': results})


# ---------------------------------------------------------------------------
# Offline self-reported payments (bank transfer / POS / cash) with a proof
# upload. A student submits -> the row lands `pending` -> a rep/admin reviews
# it. Money is never credited on the student's word alone.
# ---------------------------------------------------------------------------


def _proof_error(proof):
    """Why an uploaded proof is unacceptable, or None when it is fine."""
    if proof.size > PROOF_MAX_BYTES:
        return 'Proof file is too large (5 MB maximum).'
    if os.path.splitext(proof.name)[1].lower() not in PROOF_EXTENSIONS:
        return 'Proof must be a JPG, PNG, WEBP or PDF file.'
    return None


def _can_review(user, payment):
    """
    May this rep/admin see and decide this self-reported payment?

    Nobody reviews their own payment — a class rep is also a student who owes
    dues. Admins are otherwise unrestricted; an executive is held to their
    own department; a class rep to their own department AND level.
    """
    if payment.student_id == user.id or payment.contribution_id is None:
        return False
    if is_admin(user):
        return True
    contribution = payment.contribution
    if user.department_id is None or contribution.department_id != user.department_id:
        return False
    return user.role == 'executive' or contribution.target_level in (None, user.level)


def _review_queue(user):
    """Pending self-reports this user is allowed to decide."""
    qs = Payment.objects.filter(
        status=Payment.STATUS_PENDING,
        method=Payment.METHOD_MANUAL,
        contribution__isnull=False,
    ).exclude(student=user).select_related('student', 'contribution')

    if is_admin(user):
        return qs
    if user.department_id is None:
        return qs.none()
    qs = qs.filter(contribution__department_id=user.department_id)
    if user.role == 'executive':
        return qs  # the whole department, every level
    return qs.filter(
        Q(contribution__target_level__isnull=True)
        | Q(contribution__target_level=user.level)
    )


class SubmitOfflinePaymentView(APIView):
    """
    POST /api/payments/submit/ (student, multipart)

    The amount always comes from the contribution, never the client, and the
    row is only credited once a rep/admin approves it (ReviewPaymentView).
    """

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        channel = str(request.data.get('channel') or '').strip()
        if channel not in dict(Payment.CHANNEL_CHOICES):
            return _error(
                'bad_request', 'channel must be bank_transfer, pos or cash.', 400
            )

        proof = request.FILES.get('proof')
        if proof:
            problem = _proof_error(proof)
            if problem:
                return _error('bad_request', problem, 400)

        contribution, error = _contribution_or_error(request)
        if error is not None:
            return error

        user = request.user
        # One live self-report per student+fee: re-submitting replaces it, so a
        # retried submission never piles up duplicate rows.
        payment = Payment.objects.filter(
            student=user,
            contribution=contribution,
            method=Payment.METHOD_MANUAL,
            status=Payment.STATUS_PENDING,
        ).first() or Payment(
            student=user,
            contribution=contribution,
            payment_type=Payment.PAYMENT_CONTRIBUTION,
            amount=contribution.amount,
            method=Payment.METHOD_MANUAL,
            status=Payment.STATUS_PENDING,
            reference=f'SELF-{contribution.id}-{user.id}-{uuid.uuid4().hex[:12]}',
        )

        payment.channel = channel
        payment.note = str(request.data.get('note') or '').strip()[:255]
        if proof:
            # Stored under a random name: the client's filename is never trusted.
            proof.name = uuid.uuid4().hex + os.path.splitext(proof.name)[1].lower()
            payment.proof = proof
        payment.save()

        return Response(
            {
                'message': 'Submitted for review.',
                'payment': PaymentSerializer(payment, context={'request': request}).data,
            },
            status=201,
        )


class PendingPaymentsView(APIView):
    """GET /api/payments/pending/ — rep/admin queue of self-reports awaiting review."""

    permission_classes = [permissions.IsAuthenticated, IsClassRepOrAdmin]

    def get(self, request):
        results = PendingPaymentSerializer(
            _review_queue(request.user), many=True, context={'request': request}
        ).data
        return Response({'count': len(results), 'results': results})


class ReviewPaymentView(APIView):
    """
    POST /api/payments/{id}/review/ — approve or reject a self-reported payment.

    Approving credits it (and notifies the student); rejecting marks it failed.
    """

    permission_classes = [permissions.IsAuthenticated, IsClassRepOrAdmin]

    def post(self, request, pk):
        action = str(request.data.get('action') or '').strip()
        note = str(request.data.get('note') or '').strip()
        if action not in ('approve', 'reject'):
            return _error('bad_request', "action must be 'approve' or 'reject'.", 400)

        with transaction.atomic():
            payment = (
                Payment.objects.select_for_update()
                .filter(pk=pk, method=Payment.METHOD_MANUAL, status=Payment.STATUS_PENDING)
                .first()
            )
            if payment is None:
                return _error('not_found', 'No pending offline payment with that id.', 404)
            if not _can_review(request.user, payment):
                return _error('forbidden', 'You cannot review that payment.', 403)

            if action == 'approve':
                # Never double-credit: an existing success is an honest 409.
                if Payment.objects.filter(
                    student_id=payment.student_id,
                    contribution_id=payment.contribution_id,
                    status=Payment.STATUS_SUCCESS,
                ).exists():
                    return _error(
                        'already_paid',
                        'This student already has a successful payment for this contribution.',
                        409,
                    )
                payment.status = Payment.STATUS_SUCCESS
                if note:
                    payment.receipt_reference = note[:50]
            else:
                payment.status = Payment.STATUS_FAILED
                if note:
                    payment.note = note[:255]

            # Who decided, either way — the audit trail for offline money.
            payment.recorded_by = request.user
            _save_settled(payment)

        return Response({
            'message': 'Payment reviewed.',
            'payment': PaymentSerializer(payment, context={'request': request}).data,
        })


class PaymentProofView(APIView):
    """
    GET /api/payments/{id}/proof/ — the uploaded proof file.

    Served through the API (never a public media URL) so only the student who
    uploaded it, or a rep/admin entitled to review it, can open it.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        payment = (
            Payment.objects.filter(pk=pk).select_related('contribution').first()
        )
        allowed = payment is not None and bool(payment.proof) and (
            payment.student_id == request.user.id
            or (
                IsClassRepOrAdmin().has_permission(request, self)
                and _can_review(request.user, payment)
            )
        )
        if not allowed or not payment.proof.storage.exists(payment.proof.name):
            return _error('not_found', 'Proof not found.', 404)
        return FileResponse(payment.proof.open('rb'))
