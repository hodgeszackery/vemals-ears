"""The prompt vocabulary, and the pronunciation rewrites behind it.

The module shipped untested. These cover the half that can silently corrupt
a caption - a rewrite rule that fires on ordinary speech puts a name in
somebody's mouth, which is worse than spelling their name wrong.
"""

import unittest

from VoiceCaptions.stt.vocabulary import (
    DEFAULT_WOW_TERMS,
    MAX_PROMPT_CHARS,
    MAX_TERMS,
    PRONUNCIATIONS,
    apply_pronunciations,
    build_wow_prompt,
    normalize_terms,
)


class PronunciationTests(unittest.TestCase):
    def test_the_spoken_form_becomes_the_written_name(self):
        self.assertEqual(
            "Keihas needs a battle rez",
            apply_pronunciations("key hoss needs a battle rez"),
        )

    def test_case_does_not_matter_and_the_name_always_wins(self):
        for heard in ("Key Hoss", "KEY HOSS", "key hoss", "Key hoss"):
            with self.subTest(heard=heard):
                self.assertEqual("Keihas", apply_pronunciations(heard))

    def test_one_word_and_two_word_hearings_reach_the_same_name(self):
        for heard in ("keyhoss", "key hoss", "key  hoss", "kehoss"):
            with self.subTest(heard=heard):
                self.assertEqual("Keihas", apply_pronunciations(heard))

    def test_it_never_fires_inside_a_longer_word(self):
        # The failure that matters: inventing a name nobody said.
        for safe in (
            "keyhossler",
            "monkeyhoss",
            "keyhossium",
            "turnkeyhoss",
        ):
            with self.subTest(safe=safe):
                self.assertEqual(safe, apply_pronunciations(safe))

    def test_ordinary_speech_is_untouched(self):
        for line in (
            "hand me the key",
            "the boss is at fifty percent",
            "hoss",
            "key",
            "press the key now",
            "stack on the healer",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, apply_pronunciations(line))

    def test_punctuation_and_possessives_survive(self):
        self.assertEqual("Keihas, soak it", apply_pronunciations("key hoss, soak it"))
        self.assertEqual("Keihas's turn", apply_pronunciations("key hoss's turn"))
        self.assertEqual("(Keihas)", apply_pronunciations("(key hoss)"))

    def test_the_already_correct_spelling_is_left_alone(self):
        self.assertEqual("Keihas", apply_pronunciations("Keihas"))

    def test_every_occurrence_in_a_line_is_corrected(self):
        self.assertEqual(
            "Keihas and Keihas again",
            apply_pronunciations("key hoss and keyhoss again"),
        )

    def test_unusable_input_is_returned_unchanged(self):
        self.assertEqual("", apply_pronunciations(""))
        self.assertIsNone(apply_pronunciations(None))
        self.assertEqual(7, apply_pronunciations(7))

    def test_an_absurd_length_is_refused_rather_than_processed(self):
        huge = "key hoss " * 5000
        self.assertEqual(huge, apply_pronunciations(huge))

    def test_no_variant_is_short_enough_to_be_dangerous(self):
        # A two or three letter variant would rewrite ordinary speech. This
        # is the rule that keeps a future addition from being reckless.
        for canonical, variants in PRONUNCIATIONS.items():
            for variant in variants:
                with self.subTest(canonical=canonical, variant=variant):
                    self.assertGreaterEqual(
                        len(variant.replace(" ", "")),
                        5,
                        "variant is too short to be safe against normal speech",
                    )

    def test_every_canonical_name_is_also_offered_to_the_prompt(self):
        # The rewrite is the safety net; the prompt is the first chance.
        # A name in one and not the other is a half-wired fix.
        terms = {term.casefold() for term in DEFAULT_WOW_TERMS}
        for canonical in PRONUNCIATIONS:
            with self.subTest(canonical=canonical):
                self.assertIn(canonical.casefold(), terms)


class PromptTests(unittest.TestCase):
    def test_the_name_reaches_the_prompt(self):
        self.assertIn("Keihas", build_wow_prompt())

    def test_the_prompt_stays_within_its_bound(self):
        self.assertLessEqual(len(build_wow_prompt()), MAX_PROMPT_CHARS)
        crowded = build_wow_prompt(tuple(f"term{index}" for index in range(200)))
        self.assertLessEqual(len(crowded), MAX_PROMPT_CHARS)

    def test_extra_terms_are_carried(self):
        self.assertIn("Thrall", build_wow_prompt(("Thrall",)))

    def test_normalize_deduplicates_case_insensitively_and_bounds_count(self):
        self.assertEqual(("Keihas",), normalize_terms(("Keihas", "keihas", "KEIHAS")))
        self.assertLessEqual(
            len(normalize_terms(tuple(f"t{index}" for index in range(500)))),
            MAX_TERMS,
        )


if __name__ == "__main__":
    unittest.main()
