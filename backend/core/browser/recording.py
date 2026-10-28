"""Recording a task the person does once (BRW1, D328, F1141; DESIGN §v5.2).

While the Record banner shows, each of the person's actions in the browser is a step: open an address, click, type,
press Enter, choose, go back. Its target is what the page says about the element (role, name, label, its text, a CSS
path), so replay (F1143) can find it again by meaning first and by position last; each step gets a picture of the page
after it. Typing into one field is one step. What the person types into a password, card or one-time-code field, or
anything that reads as a card number, is never kept: the step says "You sign in here" / "You enter the card details",
and replay hands the wheel over there. `line_of` is the plain sentence the editor, the docs and the agent's tool show.
"""
from __future__ import annotations

import base64
import uuid
from urllib.parse import urlsplit

from core.browser import gate

ACTIONS = frozenset({"open", "click", "type", "press", "choose", "back"})
#: The most steps a recording holds: its stop crosses the API in one answer, pictures inline (BRW3, F1142).
MAX_STEPS = 200
FULL_WORDS = f"This recording is full: {MAX_STEPS} steps. Stop it to keep it."

#: What a step's target is, read in the page (`el` the element acted on).
TARGET_JS = """(el) => {
  if (!el || !el.tagName) return null;
  const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
  const implicit = {a: 'link', button: 'button', select: 'combobox', textarea: 'textbox', option: 'option'};
  const inputRole = ['checkbox', 'radio'].includes(type) ? type : (['submit', 'button', 'reset', 'image'].includes(type)
    ? 'button' : 'textbox');
  const role = el.getAttribute('role') || (tag === 'input' ? inputRole : implicit[tag] || '');
  const label = ((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
    || el.getAttribute('placeholder') || '').trim().slice(0, 80);
  const name = (el.getAttribute('aria-label') || (['input', 'textarea', 'select'].includes(tag) ? label : el.innerText)
    || el.value || el.getAttribute('title') || el.getAttribute('alt') || '').trim().slice(0, 80);
  const path = [];
  for (let n = el; n && n.nodeType === 1 && path.length < 4; n = n.parentElement) {
    if (n.id && /^[A-Za-z][\\w-]{0,40}$/.test(n.id)) { path.unshift('#' + n.id); break; }
    const same = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [n];
    path.unshift(n.tagName.toLowerCase() + (same.length > 1 ? `:nth-of-type(${same.indexOf(n) + 1})` : ''));
  }
  const field = ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable;
  return {role, name, label, text: field ? '' : (el.innerText || '').trim().slice(0, 80), css: path.join(' > '),
          tag, type, autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
          field: !!field && !['checkbox', 'radio', 'submit', 'button', 'reset', 'image'].includes(type)};
}"""

_PERSON_LINES = {"password": "You sign in here", "card": "You enter the card details", "code": "You enter the code"}


def sensitive_kind(target: dict | None) -> str | None:
    """`password`, `card` or `code` for a field that is the person's alone; None otherwise."""
    if not target:
        return None
    kind, auto = (target.get("type") or "").lower(), (target.get("autocomplete") or "").lower()
    if kind == "password" or "password" in auto:
        return "password"
    if any(t.startswith("cc-") for t in auto.split()):
        return "card"
    if "one-time-code" in auto:
        return "code"
    return None


def page_of(url: str) -> str:
    """A page as a step's wait names it: scheme, host and path."""
    u = urlsplit(url or "")
    return f"{u.scheme}://{u.netloc}{u.path or '/'}"


def _where(target: dict | None) -> str:
    target = target or {}
    return target.get("label") or target.get("name") or target.get("text") or target.get("css") or "the page"


def line_of(step: dict) -> str:
    """The sentence a step reads as: what the editor shows (unless renamed), and the docs and the agent's tool use."""
    action, value = step.get("action"), step.get("value") or {}
    if "person" in value:
        return _PERSON_LINES.get(value["person"], "You enter it")
    where = _where(step.get("target"))
    if action == "open":
        return f"Open {step.get('url') or 'the page'}"
    if action == "click":
        return f"Click {where}"
    if action == "type":
        said = f"{{{value['input']}}}" if "input" in value else f"“{value.get('fixed', '')}”"
        return f"Type {said} into {where}"
    if action == "press":
        return f"Press {step.get('key') or 'Enter'}"
    if action == "choose":
        return f"Choose “{value.get('fixed', '')}” in {where}"
    if action == "back":
        return "Go back"
    if action == "read":
        return f"Read {where} → {step.get('output') or 'a value'}"
    return where


class Recorder:
    """One browser's recording: its steps, their pictures, and the field being typed into."""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.shots: dict[str, bytes] = {}
        self.typing: dict | None = None     # the field the person is typing into: its step, still growing
        self.focused: dict | None = None    # the field the last click landed in
        self.holding: str | None = None     # a field whose typing is not kept, while the person types there
        self.quiet_url: str | None = None   # a page something of the person's was typed on: no pictures of it (PCI)
        self.full = False
        self._url = ""                      # the page the person is on, as the host last said
        self._from: dict[str, str] = {}     # each step's page when it was made, to learn where it led

    def at(self, url: str) -> None:
        """The page now, as a step starts (or the recording stops): where the step before led. An address it changed
        to is that step's `wait`, which replay checks before going on (scheme, host and path; a query may hold a
        typed value)."""
        last = self.steps[-1] if self.steps else None
        before = self._from.get(last["id"]) if last is not None else None
        if (last is not None and url and before and last["action"] != "open" and not last.get("wait")
                and page_of(url) != page_of(before)):
            last["wait"] = {"for": "url", "url": page_of(url)}
        self._url = url or self._url

    def sent(self, method: str, host: str) -> None:
        """A send the person's page made (method and host, never the body): an unattended replay sends only to
        these (decision #11)."""
        last = self.steps[-1] if self.steps else None
        said = f"{method.upper()} {host}"
        if last is not None and said not in last.setdefault("sends", []):
            last["sends"].append(said)

    def _add(self, step: dict) -> dict | None:
        if len(self.steps) >= MAX_STEPS:  # refused, said on the frames, never cut off silently
            self.full = True
            return None
        step = {"id": uuid.uuid4().hex[:10], "n": len(self.steps) + 1, "actor": "person", "wait": None,
                "after_shot": None, **step}
        step["line"] = line_of(step)
        self.steps.append(step)
        self._from[step["id"]] = self._url
        return step

    def flush(self) -> None:
        self.typing, self.holding = None, None

    def opened(self, url: str) -> dict:
        self.flush()
        return self._add({"action": "open", "url": url, "target": None, "value": None})

    def clicked(self, target: dict | None) -> dict | None:
        """A click: on a text field it only puts the cursor there (its typing is the step); else a click step."""
        self.flush()
        if target and target.get("field") and target.get("tag") != "select":
            self.focused = target
            return None
        self.focused = None
        return self._add({"action": "click", "target": target, "value": None})

    def typed(self, text: str, target: dict | None) -> dict | None:
        """Typing: into one field, one step whose value grows; a field that is the person's keeps no text."""
        target = target or self.focused
        if not text:
            return None
        kind = sensitive_kind(target)
        if self.typing is not None and self.typing.get("target") == target:
            step = self.typing
        else:
            self.flush()
            step = self._add({"action": "type", "target": target, "value": {"fixed": ""}})
            if step is None:
                return None
            self.typing = step
        if kind or "person" in (step["value"] or {}):
            step["value"] = {"person": kind or step["value"]["person"]}
            self.holding = step["value"]["person"]
        else:
            grown = step["value"].get("fixed", "") + text
            if gate.looks_like_card(grown):  # a card number typed anywhere is never kept
                step["value"], self.holding = {"person": "card"}, "card"
            else:
                step["value"] = {"fixed": grown}
        step["line"] = line_of(step)
        return step

    def pressed(self, key: str) -> dict | None:
        if key not in ("Enter", "NumpadEnter"):
            return None
        target, self.typing = (self.typing or {}).get("target"), None
        self.holding = None
        return self._add({"action": "press", "key": "Enter", "target": target, "value": None})

    def chose(self, target: dict | None, label: str) -> dict:
        self.flush()
        return self._add({"action": "choose", "target": target, "value": {"fixed": label}})

    def went_back(self) -> dict:
        self.flush()
        return self._add({"action": "back", "target": None, "value": None})

    def keep_quiet(self, url: str) -> None:
        """Something of the person's was typed on `url`: this page is never pictured, until the address changes."""
        self.quiet_url = url

    def shot(self, step: dict | None, jpeg: bytes | None, url: str = "") -> None:
        """The page after a step. None while a field of the person's is held, or on a page one was typed on (its
        number may still show); the quiet lifts when the address changes."""
        if self.quiet_url is not None and url and url != self.quiet_url:
            self.quiet_url = None
        if step is None or not jpeg or self.holding or self.quiet_url is not None or "person" in (step.get("value") or {}):
            return
        ref = f"r{step['n']}-{step['id'][:6]}"
        self.shots[ref] = jpeg
        step["after_shot"] = ref

    def frame(self) -> dict:
        last = self.steps[-1] if self.steps else None
        return {"steps": len(self.steps), **({"last": {"id": last["id"], "title": last["line"]}} if last else {}),
                **({"holding": self.holding} if self.holding else {}),
                **({"full": True, "words": FULL_WORDS} if self.full else {})}

    def result(self, url: str = "") -> dict:
        self.flush()
        self.at(url)
        return {"steps": self.steps, "shots": {ref: base64.b64encode(jpeg).decode() for ref, jpeg in self.shots.items()}}
