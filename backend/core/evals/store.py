"""
Rubrics and golden sets — the documents a person authors.

Both live in `collections`, the substrate skills and widgets use, because both
are fetched by their own id and nothing else. The *evidence* about runs —
scores, thumbs, suite results — is queried and aggregated, so it lives in real
tables; see `core/store/evals.py` and `CollectionDB`'s own docstring for the
rule that splits them.

The one rule worth stating out loud
-----------------------------------
**A rubric's `version` is bumped only when the scoring surface changes**, and
what counts as the scoring surface is `criteria` and `scale` — the things that
determine what a number means. Renaming a rubric, rewording its description, or
retuning `floor`/`drop` does not bump it, because none of those makes an older
score incomparable: the thresholds decide what is *flagged*, not what is
*measured*.

Getting this wrong in either direction is a real cost. Bumping too eagerly
fragments the history into windows too short to have a baseline, and the `drop`
rule goes quiet exactly when someone is iterating. Bumping too rarely lets the
dashboard compare answers to two different questions and call the difference a
regression.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

RUBRICS = "rubrics"
SUITES = "eval_suites"

#: The fields that decide what a score *means*. A change to any of them mints a
#: new version — see the module docstring.
SCORING_SURFACE = ("criteria", "scale")

MAX_NAME_CHARS = 200
MAX_TEXT_CHARS = 4_000
MAX_CRITERIA = 20
MAX_CASES = 100

DEFAULT_SCALE = {"min": 0.0, "max": 10.0}

#: How many recent runs a rolling baseline averages, when the rubric does not
#: say. Ten is long enough to absorb one unlucky run and short enough that a
#: genuine improvement becomes the new normal within a working day.
DEFAULT_BASELINE_RUNS = 10


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── rubrics ──────────────────────────────────────────────────────────────────

def clean_rubric(rubric: dict) -> dict:
    """Bound every authored field, and normalise the shapes the judge reads.

    Done here rather than in the route so the judge's prompt builder can assume
    them: a criterion with no id or a scale whose max is below its min would
    otherwise surface as a confusing model answer rather than a rejected save.
    """
    out = dict(rubric)
    out["name"] = str(out.get("name") or "Untitled rubric")[:MAX_NAME_CHARS]
    out["description"] = str(out.get("description") or "")[:MAX_TEXT_CHARS]

    criteria = out.get("criteria")
    if not isinstance(criteria, list):
        criteria = []
    cleaned: list[dict] = []
    for item in criteria[:MAX_CRITERIA]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            weight = float(item.get("weight", 1) or 1)
        except (TypeError, ValueError):
            weight = 1.0
        cleaned.append({
            "id": str(item.get("id") or _new_id("crit")),
            "name": name[:MAX_NAME_CHARS],
            "weight": max(0.0, weight),
            "guidance": str(item.get("guidance") or "")[:MAX_TEXT_CHARS],
        })
    out["criteria"] = cleaned

    scale = out.get("scale") if isinstance(out.get("scale"), dict) else {}
    try:
        lo = float(scale.get("min", DEFAULT_SCALE["min"]))
        hi = float(scale.get("max", DEFAULT_SCALE["max"]))
    except (TypeError, ValueError):
        lo, hi = DEFAULT_SCALE["min"], DEFAULT_SCALE["max"]
    if hi <= lo:
        lo, hi = DEFAULT_SCALE["min"], DEFAULT_SCALE["max"]
    out["scale"] = {"min": lo, "max": hi}

    for key in ("floor", "drop"):
        value = out.get(key)
        if value in (None, ""):
            out[key] = None
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            out[key] = None

    try:
        out["baseline_runs"] = max(1, int(out.get("baseline_runs") or DEFAULT_BASELINE_RUNS))
    except (TypeError, ValueError):
        out["baseline_runs"] = DEFAULT_BASELINE_RUNS

    out["judge_model"] = str(out.get("judge_model") or "")
    return out


def scoring_surface(rubric: dict) -> tuple:
    """The part of a rubric a score's comparability depends on.

    Criterion **ids are excluded deliberately**. They are internal handles, and
    comparing them made every save look like a scoring change: a client that
    does not echo an id back gets a freshly minted one from `clean_rubric`, so
    a pure rename bumped the version and fragmented the history into windows
    too short to have a baseline. What decides comparability is what the judge
    is *asked* — the names, weights and guidance — and the scale it answers on.
    """
    criteria = tuple(
        (c.get("name"), c.get("weight"), c.get("guidance"))
        for c in (rubric.get("criteria") or [])
    )
    scale = rubric.get("scale") or {}
    return (criteria, scale.get("min"), scale.get("max"))


def _carry_criterion_ids(incoming: dict, existing: dict) -> None:
    """Keep a criterion's id across an edit that did not send one back.

    Subscores are keyed by criterion id (`eval_scores.per_criterion`), so a
    churning id makes every historical breakdown unreadable even inside one
    rubric version — the row still says 8/10 for `crit_9f3a`, and nothing on
    the screen knows what `crit_9f3a` was. Matching by name is enough: two
    criteria with the same name are the same question however they were typed.
    """
    by_name = {
        c.get("name"): c.get("id")
        for c in (existing.get("criteria") or [])
        if c.get("name") and c.get("id")
    }
    seen: set[str] = set()
    for criterion in incoming.get("criteria") or []:
        carried = by_name.get(criterion.get("name"))
        if carried and carried not in seen:
            criterion["id"] = carried
            seen.add(carried)


async def load_rubrics() -> list[dict]:
    from core.store import collections
    return await collections.load(RUBRICS)


async def get_rubric(rubric_id: str) -> Optional[dict]:
    if not rubric_id:
        return None
    return next((r for r in await load_rubrics() if r.get("id") == rubric_id), None)


async def save_rubric(rubric: dict) -> dict:
    """Create or update, minting a new version when the scoring surface moved."""
    from core.store import collections

    data = await load_rubrics()
    rubric = clean_rubric(rubric)
    now = _now()

    idx = next((i for i, r in enumerate(data) if r.get("id") == rubric.get("id")), None)
    if idx is None:
        rubric["id"] = rubric.get("id") or _new_id("rub")
        rubric["version"] = 1
        rubric["created_at"] = now
        rubric["updated_at"] = now
        data.append(rubric)
    else:
        existing = data[idx]
        _carry_criterion_ids(rubric, existing)
        rubric["created_at"] = existing.get("created_at", now)
        rubric["updated_at"] = now
        previous = int(existing.get("version") or 1)
        # The version is the store's to decide, never the caller's: a client
        # that could set it could make two different rubrics claim to be the
        # same one, and every score taken under either becomes unreadable.
        rubric["version"] = (
            previous + 1 if scoring_surface(rubric) != scoring_surface(existing) else previous
        )
        data[idx] = rubric

    await collections.save(RUBRICS, data)
    return rubric


async def delete_rubric(rubric_id: str) -> bool:
    """Forget the rubric. Scores taken under it are deliberately left alone.

    They carry their own `rubric_id` and `rubric_version` and remain a true
    record of what was judged; deleting them would be rewriting history to
    tidy up, which is the thing this feature exists not to do.
    """
    from core.store import collections

    data = await load_rubrics()
    remaining = [r for r in data if r.get("id") != rubric_id]
    if len(remaining) == len(data):
        return False
    await collections.save(RUBRICS, remaining)
    return True


# ── golden sets ──────────────────────────────────────────────────────────────

def clean_suite(suite: dict) -> dict:
    out = dict(suite)
    out["name"] = str(out.get("name") or "Untitled set")[:MAX_NAME_CHARS]
    out["orchestration_id"] = str(out.get("orchestration_id") or "")

    cases = out.get("cases")
    if not isinstance(cases, list):
        cases = []
    cleaned: list[dict] = []
    for item in cases[:MAX_CASES]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("input") or "").strip()
        if not text:
            continue
        cleaned.append({
            "id": str(item.get("id") or _new_id("case")),
            "name": str(item.get("name") or "")[:MAX_NAME_CHARS],
            "input": text[:MAX_TEXT_CHARS],
            "notes": str(item.get("notes") or "")[:MAX_TEXT_CHARS],
        })
    out["cases"] = cleaned
    return out


async def load_suites() -> list[dict]:
    from core.store import collections
    return await collections.load(SUITES)


async def get_suite(suite_id: str) -> Optional[dict]:
    if not suite_id:
        return None
    return next((s for s in await load_suites() if s.get("id") == suite_id), None)


async def save_suite(suite: dict) -> dict:
    from core.store import collections

    data = await load_suites()
    suite = clean_suite(suite)
    now = _now()

    idx = next((i for i, s in enumerate(data) if s.get("id") == suite.get("id")), None)
    if idx is None:
        suite["id"] = suite.get("id") or _new_id("set")
        suite["created_at"] = now
        suite["updated_at"] = now
        data.append(suite)
    else:
        suite["created_at"] = data[idx].get("created_at", now)
        suite["updated_at"] = now
        data[idx] = suite

    await collections.save(SUITES, data)
    return suite


async def delete_suite(suite_id: str) -> bool:
    from core.store import collections

    data = await load_suites()
    remaining = [s for s in data if s.get("id") != suite_id]
    if len(remaining) == len(data):
        return False
    await collections.save(SUITES, remaining)
    return True
