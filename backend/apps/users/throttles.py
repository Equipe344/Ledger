from django.db.models import Q
from rest_framework.throttling import SimpleRateThrottle

from .models import User


class AccountThrottle(SimpleRateThrottle):
    """
    Limits attempts against ONE account, wherever they come from.

    The per-IP limit alone fails both ways on a campus network: hundreds of
    students share one address (so it must be generous), while an attacker
    spreads guesses across many addresses (so it protects nobody). Keying on
    the account being targeted fixes both. The submitted identifier is
    resolved to the user it names, so alternating username / email / matric
    number for the same person still lands in one bucket.
    """

    scope = 'auth_account'

    def get_cache_key(self, request, view):
        # A signed-in caller confirming their own password (e.g. handover)
        # is limited on their own account.
        if request.user and request.user.is_authenticated:
            return self.cache_format % {
                'scope': self.scope, 'ident': f'user-{request.user.pk}'
            }
        data = request.data
        identifier = ''
        for field in ('matric_number', 'email', 'username'):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                identifier = value.strip().lower()
                break
        if not identifier:
            return None  # nothing to key on; the per-IP limit still applies

        user_id = User.objects.filter(
            Q(username__iexact=identifier)
            | Q(email__iexact=identifier)
            | Q(matric_number__iexact=identifier)
        ).values_list('pk', flat=True).first()
        ident = f'user-{user_id}' if user_id else f'unknown-{identifier[:100]}'
        return self.cache_format % {'scope': self.scope, 'ident': ident}
