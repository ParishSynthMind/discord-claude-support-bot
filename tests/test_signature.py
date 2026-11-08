import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from support_bot.signature import BadSignature, Verifier, load_public_key, public_hex, sign

KEY = Ed25519PrivateKey.generate()
PUB = public_hex(KEY)
BODY = b'{"type":1}'


def ts():
    return str(int(time.time()))


def test_valid_signature_passes():
    t = ts()
    Verifier(PUB).check(sign(KEY, t, BODY), t, BODY)


def test_body_changed_fails():
    t = ts()
    with pytest.raises(BadSignature):
        Verifier(PUB).check(sign(KEY, t, BODY), t, b'{"type":2}')


def test_timestamp_changed_fails():
    t = ts()
    with pytest.raises(BadSignature):
        Verifier(PUB).check(sign(KEY, t, BODY), str(int(t) + 1), BODY)


def test_signed_by_other_key_fails():
    t = ts()
    with pytest.raises(BadSignature):
        Verifier(PUB).check(sign(Ed25519PrivateKey.generate(), t, BODY), t, BODY)


@pytest.mark.parametrize("sig,stamp", [(None, "1"), ("", "1"), ("ab", None), ("zz" * 64, "1"), ("ab" * 10, "1")])
def test_missing_or_malformed_headers_fail(sig, stamp):
    with pytest.raises(BadSignature):
        Verifier(PUB).check(sig, stamp, BODY)


def test_old_timestamp_fails_when_age_check_on():
    t = str(int(time.time()) - 3600)
    with pytest.raises(BadSignature, match="too old"):
        Verifier(PUB, max_age_s=300).check(sign(KEY, t, BODY), t, BODY)


def test_old_timestamp_allowed_when_age_check_off():
    t = str(int(time.time()) - 3600)
    Verifier(PUB, max_age_s=0).check(sign(KEY, t, BODY), t, BODY)


def test_bad_public_key_rejected():
    with pytest.raises(ValueError):
        load_public_key("not-hex")
    with pytest.raises(ValueError):
        load_public_key("ab" * 31)
