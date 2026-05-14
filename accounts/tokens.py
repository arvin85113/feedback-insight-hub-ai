from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_SIGNER = TimestampSigner(salt="email-verification")
_MAX_AGE = 259200  # 72 hours


def make_verification_token(user):
    return _SIGNER.sign(str(user.pk))


def verify_verification_token(token):
    try:
        pk = _SIGNER.unsign(token, max_age=_MAX_AGE)
        return int(pk)
    except (BadSignature, SignatureExpired, ValueError):
        return None
