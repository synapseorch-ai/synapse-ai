"""Every address a plan writes, and whether the run was given it (F1253, GUIDE1).

TRI1's 10-29 run: asked for a post about "our synapseorch product", the planner wrote https://synapseorch.com into the
step that reads the product page, and a later run made up https://synapseorchestrator.com; nobody had given it
either. An address is where a run's work goes or comes from: a link, an email, a handle, a phone number. One the run
was never given (the person's words and answers, the workspace, its profile, what research read, a pasted export) is a
guess, asked about before any plan is shown, with the planner's value filled in (`build_plan.check_build`). Code cannot
tell the person's own site from a public one (synapseorch.com looked public to the planner), so a public site nobody
typed is asked about too (Naveen, 10-30).

A link is known by its host: the person who gave synapseorch.com gave its pages. What an earlier step writes
(`{{ state.read }}`) is the run's own, never a guess. Handles and phone numbers count only as a tool's whole input:
in words, "@" and digits mean too many other things.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

_URL = re.compile(r"\bhttps?://[^\s<>\"'`(){}\[\]|\\^]+", re.I)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}\b", re.I)
_DOMAIN = re.compile(r"(?<![\w@.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\b", re.I)
_HANDLE = re.compile(r"(?<![\w.@])@[a-z0-9_]{2,30}\b", re.I)
_PHONE = re.compile(r"\+?\d[\d ().-]{5,}\d")
_WHOLE_HANDLE = re.compile(r"^@[a-z0-9_]{2,30}$", re.I)
_WHOLE_PHONE = re.compile(r"^\+?\d[\d ().-]{5,}\d$")
#: A run's own value: `{{ state.x }}`, `{{ input.x }}`, `{{ global.x }}`, the engine's one form (F1252).
_REF = re.compile(r"\{\{\s*(?:state|input|global)\.[^{}]*\}\}")
_TRAILING = ".,;:!?"

#: What a plan says that nobody acts on: the export's left-outs, and the questions it asks (asked on their own).
_UNREAD = ("finish", "not_carried_over", "questions")
#: What the form calls each kind of address.
NOUN = {"url": "address", "email": "email address", "handle": "handle", "phone": "phone number"}
#: The form field's type for each (`humanFields.ts`).
FIELD_TYPE = {"url": "string", "email": "email", "handle": "string", "phone": "phone"}


@dataclass(frozen=True)
class Address:
    value: str
    kind: str
    #: What it is known by: a link's host without "www.", an email or a handle in lower case, a phone's digits.
    key: str
    #: Where the plan uses it, as the person reads it: '"Read the product page"', 'The agent "Writer"', 'The plan'.
    where: str

    @property
    def subject(self) -> str:
        """The question's subject in the request's seam (F1226): one question per address, never asked twice."""
        return f"address {self.key}"

    def label(self) -> str:
        """The form's question; the value itself is the field's, filled in below it (the words add it as the guess)."""
        return f"{self.where} uses this {NOUN[self.kind]}, which you haven't given me. Is it the right one?"

    def said(self) -> str:
        return f"{self.where} uses {self.value}, which you haven't given me: check it before you build."


def _host(url: str) -> str:
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _clean(text: str) -> str:
    return _REF.sub(" ", text)


def _in_words(text: str, where: str) -> list[Address]:
    text = _clean(text)
    found = [Address(u.rstrip(_TRAILING), "url", _host(u.rstrip(_TRAILING)), where) for u in _URL.findall(text)]
    found += [Address(e, "email", e.lower(), where) for e in _EMAIL.findall(_URL.sub(" ", text))]
    return [a for a in found if a.key]


def _whole(value: str, where: str) -> list[Address]:
    """A tool's whole input that is a handle or a phone number."""
    value = value.strip()
    if _WHOLE_HANDLE.match(value):
        return [Address(value, "handle", value.lower(), where)]
    if _WHOLE_PHONE.match(value) and len(_digits(value)) >= 7:
        return [Address(value, "phone", _digits(value), where)]
    return []


def _walk(value: Any, where: str, *, inputs: bool = False) -> list[Address]:
    if isinstance(value, str):
        return _in_words(value, where) + (_whole(value, where) if inputs else [])
    if isinstance(value, dict):
        return [a for k, v in value.items()
                for a in _walk(v, where, inputs=inputs or k == "tool_arguments")]
    if isinstance(value, list):
        return [a for v in value for a in _walk(v, where, inputs=inputs)]
    return []


def in_plan(draft: dict) -> list[Address]:
    """Each address the planner wrote, once, first where it is used: its steps, its edits, its agents, its schedule,
    its inner workflows, then the rest of the plan."""
    draft = {k: v for k, v in (draft or {}).items() if k not in _UNREAD}
    found: list[Address] = []
    for s in draft.pop("steps", None) or []:
        if isinstance(s, dict):
            found += _walk(s, f'"{s.get("name") or s.get("id") or "A step"}"')
    for e in draft.pop("edits", None) or []:
        if isinstance(e, dict):
            step = e.get("step") if isinstance(e.get("step"), dict) else {}
            found += _walk(e, f'"{step.get("name") or e.get("step_id") or "A step"}"')
    for a in draft.pop("agents", None) or []:
        if isinstance(a, dict):
            found += _walk(a, f'The agent "{a.get("name") or "new"}"')
    found += _walk(draft.pop("schedule", None), "The schedule")
    for w in draft.pop("inner_workflows", None) or []:
        if isinstance(w, dict):
            found += _walk(w, f'The workflow "{w.get("title") or w.get("name") or "inner"}"')
    found += _walk(draft, "The plan")
    seen: set[str] = set()
    return [a for a in found if not (a.key in seen or seen.add(a.key))]


def given(texts: Iterable[Any]) -> set[str]:
    """What every address in `texts` is known by: links and bare domains by host, emails, handles, phone digits."""
    keys: set[str] = set()
    for raw in texts:
        text = raw if isinstance(raw, str) else json.dumps(raw, default=str, ensure_ascii=False)
        if not text:
            continue
        keys |= {_host(u.rstrip(_TRAILING)) for u in _URL.findall(text)}
        emails = _EMAIL.findall(text)
        keys |= {e.lower() for e in emails}
        rest = _EMAIL.sub(" ", text)
        keys |= {d.lower()[4:] if d.lower().startswith("www.") else d.lower() for d in _DOMAIN.findall(rest)}
        keys |= {e.lower().rsplit("@", 1)[1] for e in emails}  # a person who gave their email gave its domain
        keys |= {h.lower() for h in _HANDLE.findall(rest)}
        keys |= {d for p in _PHONE.findall(rest) if len(d := _digits(p)) >= 7}
    keys.discard("")
    return keys


def unsaid(draft: dict, texts: Iterable[Any]) -> list[Address]:
    """The plan's addresses the run was never given, in the order the plan uses them."""
    known = given(texts)
    return [a for a in in_plan(draft) if a.key not in known]
