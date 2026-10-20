"""A model wallet's refusal is not called an empty wallet (F689).

Cloud's gateway refuses a call with a 402 when the wallet cannot pay for it, and since F689 that is not only an empty
wallet: a plan's free allowance with money in it does not pay for a model outside it, and the gateway's sentence
says so. Knowledge reads every 402 as `wallet_402`, and its words for it said "the wallet is empty" beside that
sentence. They now say the wallet can't pay, which is true either way; the gateway's own sentence says why.
"""

import re
from pathlib import Path

from core.knowledge.errors import WalletExhausted
from core.knowledge.report import retry_hint

KNOWLEDGE_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "knowledge.ts"
EMPTY = re.compile(r"\bempty\b", re.I)


def test_the_retry_hint_does_not_call_it_empty():
    hint = retry_hint("wallet_402").hint
    assert hint and not EMPTY.search(hint) and "can't pay" in hint


def test_a_refusal_with_no_words_of_its_own_does_not_either():
    assert not EMPTY.search(str(WalletExhausted()))


def test_the_pages_words_for_it_do_not_either():
    said = [m[1] for m in re.findall(r"""wallet_402:\s*(['"])(.*?)\1""", KNOWLEDGE_TS.read_text(encoding="utf-8"))]
    assert len(said) == 2
    assert not [s for s in said if EMPTY.search(s)], said
