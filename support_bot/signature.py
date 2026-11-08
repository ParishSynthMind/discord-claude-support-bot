"""Ed25519 check of every interaction request, as Discord documents it: the signature in
X-Signature-Ed25519 covers X-Signature-Timestamp followed by the raw request body, checked
against the application's public key. Anything that fails gets a 401. Discord sends requests
with bad signatures on purpose as a routine check and removes the endpoint if they are accepted."""
import time
from typing import Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class BadSignature(Exception):
    pass


def load_public_key(hex_key: str) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(hex_key)
    except ValueError as exc:
        raise ValueError("public key is not hex") from exc
    if len(raw) != 32:
        raise ValueError("public key must be 32 bytes (64 hex characters)")
    return Ed25519PublicKey.from_public_bytes(raw)


class Verifier:
    def __init__(self, public_key_hex: str, max_age_s: int = 300, clock: Callable[[], float] = time.time):
        self.key = load_public_key(public_key_hex)
        self.max_age_s = max_age_s
        self.clock = clock

    def check(self, signature: Optional[str], timestamp: Optional[str], body: bytes) -> None:
        if not signature or not timestamp:
            raise BadSignature("missing signature headers")
        try:
            sig = bytes.fromhex(signature)
        except ValueError as exc:
            raise BadSignature("signature is not hex") from exc
        if len(sig) != 64:
            raise BadSignature("signature has the wrong length")
        try:
            self.key.verify(sig, timestamp.encode() + body)
        except InvalidSignature as exc:
            raise BadSignature("signature does not match") from exc
        # Discord does not document a freshness window; this is our own replay guard on top.
        if self.max_age_s:
            try:
                age = abs(self.clock() - int(timestamp))
            except ValueError as exc:
                raise BadSignature("timestamp is not a number") from exc
            if age > self.max_age_s:
                raise BadSignature("timestamp too old")


def sign(private_key: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    """What Discord does on its side. Used by the mock and the tests only."""
    return private_key.sign(timestamp.encode() + body).hex()


def public_hex(private_key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
