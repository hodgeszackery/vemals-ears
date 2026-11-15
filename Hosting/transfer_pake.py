"""Balanced password-authenticated key agreement for a host-to-host transfer.

A short one-time code typed on the new PC is turned into a strong session by a
SPEKE/CPace-style exchange over the RFC 3526 3072-bit safe-prime group. A wrong
guess gives exactly one online test and nothing to grind offline: the relay and
the TLS edge only ever see group elements and confirmation tags.

Standard library only. Python's modular exponentiation is not constant time;
shares are single use and the construction needs an independent review before
any release. The release proof below is deliberately NOT derived from the
session: only the old host can produce it, and only after it has stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets


PROTOCOL_VERSION = 1
ROLE_OFFER = "offer"    # the old host, which shows the code
ROLE_CLAIM = "claim"    # the new, empty host, where the code is typed
# Identical to GuildHub.server.VIEWER_LINK_CODE_ALPHABET (no I, L, O or U); a
# test pins the two together so this module never imports the relay.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
SHARE_BYTES = 384
KEY_BYTES = 32
RELEASE_SECRET_BYTES = 32
_EXPONENT_BITS = 384
# Hash to at least 128 bits more than the field so the reduction is unbiased.
_FIELD_HASH_BYTES = (3072 + 128) // 8
_DOMAIN = b"VemalsEars host transfer v1"

# RFC 3526 section 4: 2^3072 - 2^3008 - 1 + 2^64 * ([2^2942 pi] + 1690314).
# A test recomputes this from the formula and checks p and (p-1)/2 are prime.
PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF",
    16)


class PakeError(ValueError):
    """A safe-to-display failure which never includes the code or key material."""


def _text(value, limit: int = 2048) -> bytes:
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise PakeError("The transfer session input is invalid.")
    return value.encode("ascii")


def _frame(*parts: bytes) -> bytes:
    # Length-prefix every field so no two transcripts share an encoding.
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def new_code() -> str:
    """Eight symbols = 40 bits; single use, never placed in a URL or a frame."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def parse_code(text: str) -> str:
    """Accept what a person types: any case, spaces or dashes between symbols."""
    message = "Enter the 8-character transfer code shown on the other PC."
    if not isinstance(text, str) or len(text) > 64:
        raise PakeError(message)
    code = "".join(char for char in text.upper() if char not in " -\t")
    if len(code) != CODE_LENGTH or any(char not in CODE_ALPHABET for char in code):
        raise PakeError(message)
    return code


def format_code(code: str) -> str:
    code = parse_code(code)
    return code[:4] + "-" + code[4:]


def new_offer_id() -> str:
    return secrets.token_hex(16)


def _hash_to_field(code: str, offer_id: str, public_origin: str) -> int:
    """The full-width value, before reduction: never a bare 256-bit digest."""
    material = _frame(_DOMAIN, b"generator", parse_code(code).encode("ascii"),
                      _text(offer_id, 64), _text(public_origin))
    return int.from_bytes(hashlib.shake_256(material).digest(_FIELD_HASH_BYTES), "big")


def _generator_base(code: str, offer_id: str, public_origin: str) -> int:
    base = _hash_to_field(code, offer_id, public_origin) % PRIME
    if base in (0, 1, PRIME - 1):
        raise PakeError("The transfer code cannot be used. Start a new transfer.")
    return base


def generator(code: str, offer_id: str, public_origin: str) -> int:
    """Squaring moves the hashed element into the prime-order subgroup."""
    return pow(_generator_base(code, offer_id, public_origin), 2, PRIME)


def _hkdf(salt: bytes, material: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    # RFC 5869 extract-then-expand with HMAC-SHA256.
    key = hmac.digest(salt, material, "sha256")
    output, block, counter = b"", b"", 1
    while len(output) < length:
        block = hmac.digest(key, block + info + bytes((counter,)), "sha256")
        output += block
        counter += 1
    return output[:length]


@dataclass(frozen=True, repr=False)
class SessionKeys:
    """Independent keys for one transfer; the repr never shows them."""

    role: str
    transcript: bytes       # SHA-256 of the transcript; not secret
    confirm_offer: bytes
    confirm_claim: bytes
    control_mac: bytes      # HMAC key for bundle frames; see transfer._frame_mac before reuse
    bundle_enc: bytes       # AES-256 key for the transfer bundle
    bundle_mac: bytes       # HMAC key for the transfer bundle

    def _tag(self, role: str) -> bytes:
        key = self.confirm_offer if role == ROLE_OFFER else self.confirm_claim
        return hmac.digest(key, _frame(_DOMAIN, b"confirm", role.encode("ascii"), self.transcript), "sha256")

    def confirmation(self) -> bytes:
        """This side's tag. Each role has its own key, so a reflected tag fails.

        Order matters to the relay's guess budget: the claim side sends its
        tag first, and the offer side sends nothing derived from the session
        (this tag included) until it has verified that one, reporting
        transfer_failed for a wrong tag. Otherwise a claim could test a code
        guess and leave uncounted.
        """
        return self._tag(self.role)

    def verify_confirmation(self, tag: bytes) -> bool:
        peer = ROLE_CLAIM if self.role == ROLE_OFFER else ROLE_OFFER
        return isinstance(tag, bytes) and hmac.compare_digest(self._tag(peer), tag)


class Session:
    """One side of one exchange. The exponent is dropped as soon as it is used."""

    def __init__(self, role: str, code: str, offer_id: str, public_origin: str,
                 *, version: int = PROTOCOL_VERSION) -> None:
        if role not in (ROLE_OFFER, ROLE_CLAIM) or type(version) is not int or not 0 < version < 65536:
            raise PakeError("The transfer session input is invalid.")
        self.role = role
        self._context = _frame(version.to_bytes(2, "big"), _text(offer_id, 64), _text(public_origin))
        self._exponent = 1 + secrets.randbelow((1 << _EXPONENT_BITS) - 1)
        self.share = pow(generator(code, offer_id, public_origin), self._exponent, PRIME).to_bytes(SHARE_BYTES, "big")

    def finish(self, peer_share: bytes) -> SessionKeys:
        exponent, self._exponent = self._exponent, None
        if exponent is None:
            raise PakeError("This transfer session was already used.")
        if not isinstance(peer_share, bytes) or len(peer_share) != SHARE_BYTES:
            raise PakeError("The other PC sent an invalid transfer message.")
        peer = int.from_bytes(peer_share, "big")
        # Reject the trivial elements and a share reflected straight back.
        if not 2 <= peer <= PRIME - 2 or hmac.compare_digest(peer_share, self.share):
            raise PakeError("The other PC sent an invalid transfer message.")
        # The doubled exponent removes any order-two component of the share.
        shared = pow(peer, 2 * exponent, PRIME)
        if shared in (0, 1, PRIME - 1):
            raise PakeError("The other PC sent an invalid transfer message.")
        offer, claim = (self.share, peer_share) if self.role == ROLE_OFFER else (peer_share, self.share)
        transcript = hashlib.sha256(_frame(
            _DOMAIN, b"transcript", self._context, ROLE_OFFER.encode("ascii"), offer,
            ROLE_CLAIM.encode("ascii"), claim)).digest()
        material = shared.to_bytes(SHARE_BYTES, "big")

        def key(label: bytes) -> bytes:
            return _hkdf(transcript, material, _frame(_DOMAIN, label))

        return SessionKeys(
            role=self.role, transcript=transcript,
            confirm_offer=key(b"confirm offer"), confirm_claim=key(b"confirm claim"),
            control_mac=key(b"control mac"), bundle_enc=key(b"bundle encryption"),
            bundle_mac=key(b"bundle mac"))


def _transfer_id(transfer_id: str) -> bytes:
    if (not isinstance(transfer_id, str) or len(transfer_id) != 32
            or any(char not in "0123456789abcdef" for char in transfer_id)):
        raise PakeError("The transfer identifier is invalid.")
    return bytes.fromhex(transfer_id)


def new_release_secret() -> bytes:
    """R: random, made by the old host at offer time, unrelated to the session."""
    return secrets.token_bytes(RELEASE_SECRET_BYTES)


def release_commitment(transfer_id: str, release_secret: bytes) -> bytes:
    """What the authenticated bundle carries; R cannot be computed from it."""
    if not isinstance(release_secret, bytes) or len(release_secret) != RELEASE_SECRET_BYTES:
        raise PakeError("The release proof is invalid.")
    return hashlib.sha256(_DOMAIN + b" release\x00" + _transfer_id(transfer_id) + release_secret).digest()


def verify_release(transfer_id: str, commitment: bytes, release_secret: bytes) -> bool:
    try:
        expected = release_commitment(transfer_id, release_secret)
    except PakeError:
        return False
    return isinstance(commitment, bytes) and hmac.compare_digest(expected, commitment)


RELEASE_CODE_SYMBOLS = 56
RELEASE_CODE_TYPO = ("That release code has a typing mistake. Compare it with the code on the old PC, "
                     "group by group, and type it again.")


def _release_check(release_secret: bytes) -> str:
    """Four check symbols, so a slip of the finger is caught as a typing mistake.

    They add nothing secret (R is 256 random bits) and prove nothing: they
    only tell a mistyped code from a code that belongs to another transfer.
    """
    number = int.from_bytes(hashlib.sha256(_frame(_DOMAIN, b"release code check", release_secret)).digest()[:3], "big")
    return "".join(CODE_ALPHABET[(number >> shift) & 31] for shift in (19, 14, 9, 4))


def release_code(release_secret: bytes) -> str:
    """R as typed symbols for when RELEASE never arrived: 14 groups of four.

    The first 13 groups are R itself; the last group is a check on the rest.
    """
    if not isinstance(release_secret, bytes) or len(release_secret) != RELEASE_SECRET_BYTES:
        raise PakeError("The release proof is invalid.")
    number = int.from_bytes(release_secret, "big") << 4
    symbols = "".join(CODE_ALPHABET[(number >> shift) & 31] for shift in range(255, -1, -5))
    symbols += _release_check(release_secret)
    return "-".join(symbols[index:index + 4] for index in range(0, RELEASE_CODE_SYMBOLS, 4))


def parse_release_code(text: str) -> bytes:
    message = "Enter the complete release code shown on the old PC."
    if not isinstance(text, str) or len(text) > 256:
        raise PakeError(message)
    symbols = "".join(char for char in text.upper() if char not in " -\t\r\n")
    if len(symbols) != RELEASE_CODE_SYMBOLS or any(char not in CODE_ALPHABET for char in symbols):
        raise PakeError(message)
    number = 0
    for char in symbols[:52]:
        number = (number << 5) | CODE_ALPHABET.index(char)
    if number & 15:
        raise PakeError(RELEASE_CODE_TYPO)
    release_secret = (number >> 4).to_bytes(RELEASE_SECRET_BYTES, "big")
    if not hmac.compare_digest(_release_check(release_secret), symbols[52:]):
        raise PakeError(RELEASE_CODE_TYPO)
    return release_secret
