"""
What a student is charged online when Paystack's fee is passed on to them.

Paystack takes its fee out of whatever is charged, so to leave the department
with the full fee the charge has to be grossed up — and the fee applies to the
grossed-up amount too. Paystack's published rule for local (NGN) payments:

    fee = 1.5% of the charge + NGN 100
    the NGN 100 is waived when the charge is under NGN 2,500
    the fee never exceeds NGN 2,000

The four numbers live in settings because they are Paystack's to change.
All arithmetic is in whole kobo, and every division rounds UP: the department
must never receive less than the fee it set.
"""
from decimal import ROUND_CEILING, Decimal

from django.conf import settings


def _kobo(naira):
    return int(Decimal(str(naira)) * 100)


def gateway_fee_kobo(charge_kobo):
    """Paystack's fee on a charge of `charge_kobo`, per the rule above."""
    rate = Decimal(str(settings.PAYSTACK_FEE_PERCENT)) / 100
    fee = (charge_kobo * rate).to_integral_value(rounding=ROUND_CEILING)
    if charge_kobo >= _kobo(settings.PAYSTACK_FEE_FLAT_THRESHOLD):
        fee += _kobo(settings.PAYSTACK_FEE_FLAT)
    return min(int(fee), _kobo(settings.PAYSTACK_FEE_CAP))


def online_charge(amount):
    """
    The naira amount to charge online for a fee of `amount` (Decimal, 2dp).

    With pass-through off this is simply the fee. With it on, it is the
    smallest charge that still leaves the department the full fee after
    Paystack's cut.
    """
    if not settings.PAYSTACK_PASS_FEE_TO_STUDENT:
        return amount

    fee_kobo = int(amount * 100)
    rate = Decimal(str(settings.PAYSTACK_FEE_PERCENT)) / 100
    flat = _kobo(settings.PAYSTACK_FEE_FLAT)
    threshold = _kobo(settings.PAYSTACK_FEE_FLAT_THRESHOLD)
    cap = _kobo(settings.PAYSTACK_FEE_CAP)

    def grossed_up(net_kobo):
        return int((Decimal(net_kobo) / (1 - rate)).to_integral_value(rounding=ROUND_CEILING))

    # Small payments: the flat part is waived if the final charge stays under
    # the threshold. Otherwise the flat part is grossed up along with the fee.
    charge = grossed_up(fee_kobo)
    if charge >= threshold:
        charge = min(grossed_up(fee_kobo + flat), fee_kobo + cap)

    return (Decimal(charge) / 100).quantize(Decimal('0.01'))
