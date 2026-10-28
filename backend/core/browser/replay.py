"""Replaying a recording by code (BRW1, D328, F1143; DESIGN §v5.3).

No model on the happy path: each step finds its target the way a person would describe it first and by position
last (role and name, then label, then text, then the recorded CSS path), acts, and waits for what it recorded. The
rules that keep sends the person's still hold (F1124/F1125, decision #11):

* a recorded press that acts for the person (submit, send, post…) runs because they pressed it when recording, when
  someone is there; unattended (a scheduled run nobody watches) only if that step's `may_send` is on;
* a payment, a permanent delete or closing an account is always the person's, recorded or not: replay hands them
  the wheel there;
* a step that was the person's alone ("You sign in here") hands them the wheel too.

A step that finds no target, or doesn't reach the page it expects, is a miss: replay stops there and says which, with
the page as it is, for the agent's fix (F1144) or the person.
"""
from __future__ import annotations

from core.browser import gate
from core.browser.recording import page_of  # noqa: F401  (a step's wait names its page this way)

#: How long one rung of the ladder waits for its element.
FIND_MS = 4000
#: How long a step waits for the page it recorded reaching.
WAIT_MS = 10000
#: How long a step's effects get to settle (its sends, a page load) before the next step or the replay's end.
SETTLE_MS = 3000

#: A read step's element as data: a table as rows keyed by its header, a list as its items, else its text.
READ_JS = """(el) => {
  const text = (n) => (n.innerText || n.textContent || '').trim();
  const table = el.closest('table') || el.querySelector('table');
  if (table) {
    const rows = [...table.rows].map(r => [...r.cells].map(text));
    if (!rows.length) return [];
    const head = table.tHead ? rows.shift() : (table.rows[0].querySelector('th') ? rows.shift() : null);
    return head ? rows.map(r => Object.fromEntries(head.map((h, i) => [h || `column ${i + 1}`, r[i] ?? '']))) : rows;
  }
  const list = el.closest('ul,ol') || (el.matches('ul,ol') ? el : null);
  if (list) return [...list.children].map(text);
  return text(el);
}"""


def fill_value(step: dict, inputs: dict) -> str | None:
    """What a type or choose step puts in: its fixed value, or the run's input it names; None when an input is
    missing (the step can't run) or the step is the person's."""
    value = step.get("value") or {}
    if "person" in value:
        return None
    if "input" in value:
        got = inputs.get(value["input"])
        return None if got is None else str(got)
    return str(value.get("fixed", ""))


def press_rule(step: dict, tier: str, *, unattended: bool) -> str | None:
    """Why a recorded press may not run now, or None to run it. `tier` is `gate.classify_press`'s for its target."""
    if tier == gate.MONEY:
        return "a payment, a permanent delete or closing an account is always the person's"
    if tier == gate.ACTS and unattended and not step.get("may_send"):
        return "this step may not send on its own; someone has to be there"
    return None


def locators(page, target: dict | None) -> list:
    """The ladder for a target: by role and name, by label, by text, by its CSS path; first rungs first."""
    target = target or {}
    out = []
    role, name = target.get("role"), target.get("name")
    if role and name:
        try:
            out.append(("role", page.get_by_role(role, name=name, exact=True)))
        except Exception:
            pass
    if target.get("label"):
        out.append(("label", page.get_by_label(target["label"], exact=True)))
    if target.get("text"):
        out.append(("text", page.get_by_text(target["text"], exact=True)))
    if target.get("css"):
        out.append(("css", page.locator(target["css"])))
    return out


async def find(page, target: dict | None):
    """`(locator, rung)` for the first rung that finds exactly one visible element, or `(None, None)`."""
    for rung, loc in locators(page, target):
        try:
            await loc.first.wait_for(state="visible", timeout=FIND_MS)
            if await loc.count() == 1:
                return loc, rung
        except Exception:
            continue
    return None, None


async def read(loc):
    """What a read step returns: rows, items or text, capped so one page can't flood a run."""
    got = await loc.first.evaluate(READ_JS)
    if isinstance(got, list):
        return got[:500]
    return str(got or "")[:20000]
