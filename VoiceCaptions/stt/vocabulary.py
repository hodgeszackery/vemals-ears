"""Bounded initial-prompt vocabulary for Warcraft raid callouts."""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_TERMS = 64
MAX_TERM_CHARS = 40
MAX_PROMPT_CHARS = 900

DEFAULT_WOW_TERMS = (
    # The person this was built for, and so the name most likely to be
    # spoken at him. Whisper has no reason to guess it from sound alone
    # and lands on "Vermal" or "V mal" instead, which is a poor thing for
    # somebody to read every time the raid addresses them.
    "Vemal",
    # Pronounced "Key Hoss", which is nothing like how it is spelled. The
    # prompt term below only raises the odds; PRONUNCIATIONS is what
    # actually fixes it once Whisper writes down what it heard.
    "Keihas",
    "World of Warcraft",
    "Mythic Plus",
    "Bloodlust",
    "Heroism",
    "Healthstone",
    "battle rez",
    "interrupt",
    "dispel",
    "defensive",
    "cooldown",
    "soak",
    "stack",
    "spread",
    "frontal",
    "tank buster",
    "adds",
    "melee",
    "ranged",
    "healer",
)

_SPACE = re.compile(r"\s+")

# Names whose spelling and sound diverge far enough that the initial prompt
# alone will not land them. The prompt raises the odds of the right spelling;
# this rewrites the wrong one after the fact, which is the half that actually
# holds when somebody says the name mid-sentence.
#
# Each entry is a canonical spelling mapped to what Whisper writes instead.
# Variants are matched whole-word and case-insensitively, and the canonical
# spelling always wins - these are proper names, so "key hoss" at the start
# of a sentence is still "Keihas", not "keihas".
#
# Keep variants DISTINCTIVE. A short or common variant would rewrite ordinary
# speech, and a caption that invents a name nobody said is worse than one that
# spells a name wrong.
PRONUNCIATIONS = {
    "Keihas": (
        "key hoss",
        "key hos",
        "key haas",
        "kay hoss",
        "keyhoss",
        "kehoss",
    ),
}

MAX_PRONUNCIATION_INPUT = 4000


def _variant_pattern(variant: str) -> str:
    """Match a variant whole-word, tolerating any run of spaces inside it.

    Whisper is inconsistent about whether it hears one word or two, so
    "keyhoss" and "key  hoss" have to reach the same place. The bounds are
    lookarounds on word characters rather than \\b so that a variant ending
    in a letter still refuses to match inside a longer word.
    """
    words = [re.escape(word) for word in variant.split()]
    return r"(?<![0-9A-Za-z])" + r"\s+".join(words) + r"(?![0-9A-Za-z])"


_PRONUNCIATION_RULES = tuple(
    (
        re.compile(
            "|".join(_variant_pattern(variant) for variant in variants),
            re.IGNORECASE,
        ),
        canonical,
    )
    for canonical, variants in PRONUNCIATIONS.items()
    if variants
)


def apply_pronunciations(text: str) -> str:
    """Rewrite heard-but-wrong spellings of known names to the real one.

    Applied to a caption AFTER its parts are joined, so a name split across
    a streamed part boundary is still caught - the halves are only adjacent
    once the whole line exists.

    Returns the input unchanged for anything that is not usable text, and
    refuses to work on absurd lengths rather than burning the caption path
    on a pathological input.
    """
    if not isinstance(text, str) or not text:
        return text
    if len(text) > MAX_PRONUNCIATION_INPUT:
        return text
    for pattern, canonical in _PRONUNCIATION_RULES:
        text = pattern.sub(canonical, text)
    return text


def normalize_terms(terms: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in terms:
        if not isinstance(value, str):
            continue
        clean = "".join(
            " " if char in "\t\r\n," else char
            for char in value
            if char >= " " or char in "\t\r\n"
            if char != "\x7f"
        )
        clean = _SPACE.sub(" ", clean).strip()[:MAX_TERM_CHARS].rstrip()
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= MAX_TERMS:
            break
    return tuple(result)


def build_wow_prompt(extra_terms: Iterable[str] = ()) -> str:
    terms = normalize_terms((*DEFAULT_WOW_TERMS, *tuple(extra_terms)))
    prefix = (
        "World of Warcraft raid voice callouts. Preserve these game terms "
        "and player names when heard: "
    )
    available = MAX_PROMPT_CHARS - len(prefix) - 1
    selected: list[str] = []
    used = 0
    for term in terms:
        addition = len(term) + (2 if selected else 0)
        if used + addition > available:
            break
        selected.append(term)
        used += addition
    return prefix + ", ".join(selected) + "."
