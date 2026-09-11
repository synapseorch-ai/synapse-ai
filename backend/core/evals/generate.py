"""
Proposing golden-set cases from an orchestration's own definition.

One model call, seeded with what the workflow is and what the rubric asks of
it, returning candidate inputs. **It saves nothing.** The candidates come back
to the screen, the person edits and accepts the ones they want, and only then
does a save happen.

That is the whole design, and the restraint is deliberate. A generator that
wrote straight to the set would be putting model-authored text into the thing
the model is later measured against — which is how a suite quietly starts
testing what the model finds easy rather than what the business needs. A person
in the middle is what makes a golden set golden.
"""
from __future__ import annotations

import asyncio
import json

from core.log import get_logger

_log = get_logger(__name__)

TIMEOUT_SECONDS = 45.0
MAX_CASES = 10

SYSTEM_PROMPT = (
    "You write test inputs for automated workflows. "
    "You output ONLY a JSON array of objects. No prose."
)


def build_prompt(orch: dict, rubric: dict | None, count: int, steer: str = "") -> str:
    steps = [
        f"  - {s.get('name')} ({s.get('type')})"
        for s in (orch.get("steps") or [])[:20]
    ]
    schema = orch.get("state_schema") or {}
    inputs = ", ".join(schema.keys()) if schema else "a single free-text input"

    rubric_block = ""
    if rubric:
        criteria = "\n".join(
            f"  - {c.get('name')}: {c.get('guidance') or ''}".rstrip(": ")
            for c in (rubric.get("criteria") or [])
        )
        rubric_block = (
            f"\nRuns are judged on:\n{criteria}\n"
            "Include cases that would genuinely stress those criteria, not only easy ones.\n"
        )

    steer_block = f"\nThe person asked for: {steer}\n" if steer.strip() else ""

    return (
        f"A workflow called \"{orch.get('name')}\" does this:\n"
        f"{orch.get('description') or '(no description)'}\n\n"
        f"Its steps:\n" + ("\n".join(steps) or "  (none)") + "\n\n"
        f"It takes: {inputs}\n"
        f"{rubric_block}{steer_block}\n"
        f"Write {count} varied test inputs for it. Cover the ordinary case, the "
        f"awkward edge, and at least one that should be hard.\n\n"
        f'Respond with ONLY a JSON array: '
        f'[{{"name": "<short label>", "input": "<the actual input text>", '
        f'"notes": "<what this case is checking>"}}]'
    )


def parse_cases(text: str, limit: int) -> list[dict]:
    """Read the candidates out of a reply, or return nothing.

    Nothing rather than a guess: these are about to be shown to a person as
    suggestions to accept, and a half-parsed case is worse than an empty list
    because it looks deliberate.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    payload = None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            try:
                payload = json.loads(raw[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                payload = None

    if not isinstance(payload, list):
        return []

    out: list[dict] = []
    for item in payload[:limit]:
        if not isinstance(item, dict):
            continue
        text_in = str(item.get("input") or "").strip()
        if not text_in:
            continue
        out.append({
            "name": str(item.get("name") or "")[:200],
            "input": text_in[:4_000],
            "notes": str(item.get("notes") or "")[:4_000],
        })
    return out


async def propose(orch: dict, rubric: dict | None, settings: dict,
                  count: int = 5, steer: str = "") -> list[dict]:
    """Candidate cases for a person to edit and accept. Saves nothing."""
    from core.llm_providers import detect_mode_from_model, generate_response

    count = max(1, min(count, MAX_CASES))
    model = (
        (settings.get("judge_model") or "").strip()
        or settings.get("model", "mistral")
    )
    try:
        text = await asyncio.wait_for(
            generate_response(
                prompt_msg=build_prompt(orch, rubric, count, steer),
                sys_prompt=SYSTEM_PROMPT,
                mode=detect_mode_from_model(model),
                current_model=model,
                current_settings=settings,
                agent_id="eval_case_writer",
                source="evals",
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _log.warning("case generation timed out")
        return []
    except Exception as exc:
        _log.warning(f"case generation failed: {exc}")
        return []

    return parse_cases(text if isinstance(text, str) else "", count)
