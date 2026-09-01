"""Task 3: the adversarial suite's own assertions. Every attack must come back
REFUSED or CORRECT - never FALSE_MATCH. See attacks.py for what each attack
targets and why.
"""

from __future__ import annotations

import pytest

from tests.adversarial.attacks import ALL_ATTACKS, run_all_attacks


def test_no_attack_produces_a_false_match():
    results = run_all_attacks()
    false_matches = [r for r in results if r.outcome == "FALSE_MATCH"]
    assert not false_matches, "\n".join(f"{r.attack}: {r.detail}" for r in false_matches)


def test_seven_attacks_are_defined():
    # A count check on ALL_ATTACKS itself - if an attack silently stops being
    # registered, the suite must not quietly shrink to "still passing."
    assert len(ALL_ATTACKS) == 7


@pytest.mark.parametrize("attack", ALL_ATTACKS, ids=lambda a: a.__name__)
def test_each_attack_resolves_to_a_valid_outcome(attack):
    result = attack()
    assert result.outcome in ("REFUSED", "CORRECT", "FALSE_MATCH")
    assert result.outcome != "FALSE_MATCH", result.detail
