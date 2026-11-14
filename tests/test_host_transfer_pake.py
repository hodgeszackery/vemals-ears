"""Synthetic codes and sessions only: no host state, credentials or network."""

import hashlib
import hmac
from pathlib import Path
import re
import unittest
from unittest import mock

from Hosting import transfer_pake as pake


OFFER_ID = "0123456789abcdef0123456789abcdef"
ORIGIN = "https://ears.example.org"
CODE = "XEN0-7E5T"
TRANSFER = "fedcba9876543210fedcba9876543210"


def pair(code_offer=CODE, code_claim=CODE, **claim):
    offer = pake.Session(pake.ROLE_OFFER, code_offer, OFFER_ID, ORIGIN)
    arguments = {"offer_id": OFFER_ID, "public_origin": ORIGIN, **claim}
    joiner = pake.Session(pake.ROLE_CLAIM, code_claim, **arguments)
    return offer.finish(joiner.share), joiner.finish(offer.share)


def secrets_of(keys):
    return [keys.confirm_offer, keys.confirm_claim, keys.control_mac, keys.bundle_enc, keys.bundle_mac]


def pi_scaled(bits):
    # floor(pi * 2**bits) by Machin's formula, in integers.
    one = 1 << (bits + 64)

    def arctan_inverse(value):
        total = term = one // value
        square, divisor, sign = value * value, 1, -1
        while term:
            term //= square
            divisor += 2
            total += sign * (term // divisor)
            sign = -sign
        return total

    return (4 * (4 * arctan_inverse(5) - arctan_inverse(239))) >> 64


def probably_prime(value):
    odd, shifts = value - 1, 0
    while odd % 2 == 0:
        odd //= 2
        shifts += 1
    for base in (2, 3, 5, 7, 11, 13, 17, 19):
        witness = pow(base, odd, value)
        if witness in (1, value - 1):
            continue
        for _ in range(shifts - 1):
            witness = witness * witness % value
            if witness == value - 1:
                break
        else:
            return False
    return True


class GroupTests(unittest.TestCase):
    def test_prime_is_the_rfc_3526_3072_bit_safe_prime(self):
        expected = 2 ** 3072 - 2 ** 3008 - 1 + 2 ** 64 * (pi_scaled(2942) + 1690314)
        self.assertEqual(expected, pake.PRIME)
        self.assertEqual(3072, pake.PRIME.bit_length())
        self.assertTrue(probably_prime(pake.PRIME))
        self.assertTrue(probably_prime((pake.PRIME - 1) // 2))

    def test_generator_is_hashed_to_the_full_field_width_before_squaring(self):
        self.assertGreaterEqual(pake._FIELD_HASH_BYTES * 8, 3072 + 128)
        bases = set()
        for index in range(8):
            code = pake.CODE_ALPHABET[index] * 8
            wide = pake._hash_to_field(code, OFFER_ID, ORIGIN)
            base = pake._generator_base(code, OFFER_ID, ORIGIN)
            # A bare 256- or 512-bit digest would fail both of these.
            self.assertGreater(wide.bit_length(), 3072 + 64)
            self.assertGreater(base.bit_length(), 3072 - 64)
            self.assertEqual(wide % pake.PRIME, base)
            value = pake.generator(code, OFFER_ID, ORIGIN)
            self.assertEqual(pow(base, 2, pake.PRIME), value)
            # Squaring lands in the subgroup of prime order (p - 1) / 2.
            self.assertEqual(1, pow(value, (pake.PRIME - 1) // 2, pake.PRIME))
            self.assertNotIn(value, (0, 1, pake.PRIME - 1))
            bases.add(base)
        self.assertEqual(8, len(bases))

    def test_generator_binds_code_offer_and_origin_and_rejects_trivial_values(self):
        first = pake.generator(CODE, OFFER_ID, ORIGIN)
        self.assertEqual(first, pake.generator("xen0 7e5t", OFFER_ID, ORIGIN))
        self.assertNotEqual(first, pake.generator("XEN0-7E5V", OFFER_ID, ORIGIN))
        self.assertNotEqual(first, pake.generator(CODE, "1" + OFFER_ID[1:], ORIGIN))
        self.assertNotEqual(first, pake.generator(CODE, OFFER_ID, "https://ears.example.net"))
        for trivial in (0, 1, pake.PRIME - 1, pake.PRIME, pake.PRIME + 1, 2 * pake.PRIME - 1):
            with self.subTest(trivial=trivial % pake.PRIME):
                with mock.patch.object(pake, "_hash_to_field", return_value=trivial):
                    with self.assertRaises(pake.PakeError):
                        pake.generator(CODE, OFFER_ID, ORIGIN)
                    with self.assertRaises(pake.PakeError):
                        pake.Session(pake.ROLE_OFFER, CODE, OFFER_ID, ORIGIN)


class CodeTests(unittest.TestCase):
    def test_alphabet_is_the_relay_viewer_link_alphabet(self):
        source = (Path(__file__).resolve().parents[1] / "GuildHub/server.py").read_text(encoding="utf-8")
        match = re.search(r'^VIEWER_LINK_CODE_ALPHABET = "([^"]+)"$', source, re.MULTILINE)
        self.assertEqual(match.group(1), pake.CODE_ALPHABET)
        self.assertEqual(32, len(set(pake.CODE_ALPHABET)))

    def test_codes_are_eight_symbols_shown_as_two_groups(self):
        codes = {pake.new_code() for _ in range(64)}
        self.assertEqual(64, len(codes))
        for code in codes:
            self.assertRegex(code, "^[" + pake.CODE_ALPHABET + "]{8}$")
            shown = pake.format_code(code)
            self.assertRegex(shown, "^.{4}-.{4}$")
            self.assertEqual(code, pake.parse_code(shown))

    def test_parsing_tolerates_spaces_dashes_and_case_only(self):
        for typed in ("XEN07E5T", "xen0-7e5t", " xen0 7e5t ", "X-E-N-0 7 E 5 T", "xEn0\t7e5T"):
            self.assertEqual("XEN07E5T", pake.parse_code(typed))
        for typed in ("", "XEN07E5", "XEN07E5TT", "XENO7E5T", "XEN07E5I", "XEN0_7E5T", None, 12345678,
                      "XEN0-7E5T" * 8):
            with self.subTest(typed=typed):
                with self.assertRaisesRegex(pake.PakeError, "8-character transfer code") as raised:
                    pake.parse_code(typed)
                self.assertNotIn("XEN", str(raised.exception))


class AgreementTests(unittest.TestCase):
    def test_both_sides_derive_the_same_independent_keys_and_confirm(self):
        offer, claim = pair(code_claim="xen0 7e5t")
        self.assertEqual(secrets_of(offer), secrets_of(claim))
        self.assertEqual(offer.transcript, claim.transcript)
        self.assertEqual(5, len(set(secrets_of(offer))))
        self.assertTrue(all(len(key) == 32 for key in secrets_of(offer)))
        self.assertTrue(claim.verify_confirmation(offer.confirmation()))
        self.assertTrue(offer.verify_confirmation(claim.confirmation()))
        again, _ = pair()
        self.assertNotEqual(offer.bundle_enc, again.bundle_enc)

    def test_confirmation_is_role_separated_so_a_reflected_tag_fails(self):
        offer, claim = pair()
        self.assertNotEqual(offer.confirmation(), claim.confirmation())
        self.assertFalse(offer.verify_confirmation(offer.confirmation()))
        self.assertFalse(claim.verify_confirmation(claim.confirmation()))
        tag = claim.confirmation()
        self.assertFalse(offer.verify_confirmation(tag[:-1] + bytes((tag[-1] ^ 1,))))
        self.assertFalse(offer.verify_confirmation("not bytes"))

    def test_wrong_code_fails_confirmation_and_shares_no_key(self):
        offer, claim = pair(code_claim="XEN0-7E5V")
        self.assertFalse(offer.verify_confirmation(claim.confirmation()))
        self.assertFalse(claim.verify_confirmation(offer.confirmation()))
        self.assertFalse(set(secrets_of(offer)) & set(secrets_of(claim)))

    def test_origin_offer_id_and_version_are_bound_into_the_keys(self):
        for change in ({"public_origin": "https://ears.example.net"}, {"offer_id": "f" * 32}):
            with self.subTest(change=change):
                offer, claim = pair(**change)
                self.assertFalse(offer.verify_confirmation(claim.confirmation()))
        # Same generator, different announced protocol version.
        offer = pake.Session(pake.ROLE_OFFER, CODE, OFFER_ID, ORIGIN)
        claim = pake.Session(pake.ROLE_CLAIM, CODE, OFFER_ID, ORIGIN, version=2)
        first, second = offer.finish(claim.share), claim.finish(offer.share)
        self.assertFalse(first.verify_confirmation(second.confirmation()))
        self.assertFalse(set(secrets_of(first)) & set(secrets_of(second)))

    def test_trivial_out_of_range_and_reflected_shares_are_rejected(self):
        width = pake.SHARE_BYTES
        bad = [value.to_bytes(width, "big") for value in (0, 1, pake.PRIME - 1, pake.PRIME, 2 ** 3072 - 1)]
        bad += [b"", b"\x02" * (width - 1), b"\x02" * (width + 1), "text", None]
        for share in bad:
            with self.subTest(share=repr(share)[:24]):
                session = pake.Session(pake.ROLE_OFFER, CODE, OFFER_ID, ORIGIN)
                with self.assertRaisesRegex(pake.PakeError, "invalid transfer message"):
                    session.finish(share)
        session = pake.Session(pake.ROLE_CLAIM, CODE, OFFER_ID, ORIGIN)
        with self.assertRaisesRegex(pake.PakeError, "invalid transfer message"):
            session.finish(session.share)

    def test_a_session_is_single_use_and_drops_its_exponent(self):
        offer = pake.Session(pake.ROLE_OFFER, CODE, OFFER_ID, ORIGIN)
        claim = pake.Session(pake.ROLE_CLAIM, CODE, OFFER_ID, ORIGIN)
        offer.finish(claim.share)
        self.assertIsNone(offer._exponent)
        with self.assertRaisesRegex(pake.PakeError, "already used"):
            offer.finish(claim.share)
        # A rejected share also spends the session: one test per run.
        with self.assertRaises(pake.PakeError):
            claim.finish(b"\x00" * pake.SHARE_BYTES)
        with self.assertRaisesRegex(pake.PakeError, "already used"):
            claim.finish(offer.share)

    def test_invalid_roles_and_inputs_fail_without_echoing_them(self):
        for arguments in (("host", CODE, OFFER_ID, ORIGIN), (pake.ROLE_OFFER, CODE, "", ORIGIN),
                          (pake.ROLE_OFFER, CODE, OFFER_ID, "https://ears example"),
                          (pake.ROLE_OFFER, CODE, OFFER_ID, None)):
            with self.assertRaises(pake.PakeError):
                pake.Session(*arguments)
        with self.assertRaises(pake.PakeError):
            pake.Session(pake.ROLE_OFFER, CODE, OFFER_ID, ORIGIN, version=0)
        keys, _ = pair()
        self.assertNotIn(keys.bundle_enc.hex(), repr(keys))
        self.assertNotIn(repr(keys.bundle_enc), repr(keys))

    def test_key_schedule_is_rfc_5869_hkdf_sha256(self):
        # RFC 5869 appendix A.1.
        output = pake._hkdf(bytes.fromhex("000102030405060708090a0b0c"), b"\x0b" * 22,
                            bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42)
        self.assertEqual(
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865", output.hex())


class ReleaseProofTests(unittest.TestCase):
    def test_commitment_is_domain_separated_and_bound_to_the_transfer(self):
        secret = b"\x5a" * 32
        commitment = pake.release_commitment(TRANSFER, secret)
        self.assertEqual(commitment, pake.release_commitment(TRANSFER, secret))
        self.assertNotEqual(commitment, pake.release_commitment(OFFER_ID, secret))
        self.assertNotEqual(commitment, hashlib.sha256(secret).digest())
        self.assertNotEqual(commitment, hashlib.sha256(bytes.fromhex(TRANSFER) + secret).digest())
        self.assertTrue(pake.verify_release(TRANSFER, commitment, secret))
        self.assertFalse(pake.verify_release(OFFER_ID, commitment, secret))
        self.assertFalse(pake.verify_release(TRANSFER, commitment, b"\x5b" * 32))
        for invalid in (b"", b"\x5a" * 31, "text", None, commitment + b"\x00"):
            self.assertFalse(pake.verify_release(TRANSFER, commitment, invalid))
        with self.assertRaises(pake.PakeError):
            pake.release_commitment("not-an-id", secret)
        self.assertEqual(32, len(pake.new_release_secret()))
        self.assertNotEqual(pake.new_release_secret(), pake.new_release_secret())

    def test_release_code_round_trips_the_whole_secret(self):
        for secret in (bytes(32), b"\xff" * 32, bytes(range(32)), pake.new_release_secret()):
            code = pake.release_code(secret)
            self.assertRegex(code, "^(?:[" + pake.CODE_ALPHABET + "]{4}-){12}[" + pake.CODE_ALPHABET + "]{4}$")
            self.assertEqual(secret, pake.parse_release_code(code))
            self.assertEqual(secret, pake.parse_release_code(" " + code.lower().replace("-", " ") + "\n"))
        code = pake.release_code(bytes(range(32)))
        for typed in ("", code[:-1], code + "0", code[:-1] + "I", code[:-1] + "1", None):
            with self.subTest(typed=typed):
                with self.assertRaisesRegex(pake.PakeError, "complete release code"):
                    pake.parse_release_code(typed)

    def test_release_proof_is_independent_of_the_session(self):
        # The receiver holds every session key and the commitment. None of
        # them, and no MAC of the transfer under them, opens the commitment.
        _, claim = pair()
        secret = pake.new_release_secret()
        commitment = pake.release_commitment(TRANSFER, secret)
        candidates = secrets_of(claim) + [claim.transcript, commitment]
        candidates += [hmac.digest(key, bytes.fromhex(TRANSFER), "sha256") for key in secrets_of(claim)]
        candidates += [hmac.digest(key, commitment, "sha256") for key in secrets_of(claim)]
        for candidate in candidates:
            self.assertFalse(pake.verify_release(TRANSFER, commitment, candidate))
        self.assertFalse(hasattr(claim, "release"))
        self.assertNotIn("release", " ".join(vars(claim)))


if __name__ == "__main__":
    unittest.main()
