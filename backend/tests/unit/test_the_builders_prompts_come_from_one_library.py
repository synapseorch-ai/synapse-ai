"""Synapse AI's prompts come from one library, and three prompt-form rules hold over all of them (SAI2-P1, F773).

The same paragraph was pasted into three agents and two steps (the savers' parallel section, "a tool's description
must match", the findings steps' opening), so a fix to one copy left the others as they were (F764). A passage two
places share now lives once, in `core/native_builder/prompts/<name>.md`, and each place names it as
`{{PROMPT:name}}`, expanded when the bundle is read exactly as `{{STEP_TYPE_CHEATSHEET}}` is.

Moving them changed no word: the expanded bundle is byte-for-byte what it was (its hash is pinned below; a phase that
changes a prompt on purpose updates the hash in the same commit, and says so). Three rules hold over the whole bundle:

* F757: no prompt asks for a marker block (`[REASONING]`, `[WHY]` …), which the Claude 5 family refuses;
* F764: no sentence of twelve words or more is written in two places unless both name one fragment;
* F767: a for-each loop's body step leads its prompt with the item it is working on.
"""
import hashlib
import json

import pytest

from core.native_builder import prompt_lint, seed

#: The expanded bundle. Change it only with a prompt change made on purpose, in the same commit. At v2.0 635a3967,
#: before any passage moved into the library: 0edc6f5b…; SAI2-P2 (F799): the change route on a typed Change,
#: 074d96f7…; F800: the analyst no longer told never to call a tool it does not hold, 2b9c8941…; a change's hand-over
#: goes back through the door, 7eb7dec5…; a code step's error is said, 29c31610…; too many rounds end in words,
#: 5fc3d1f8…; one new thing is made by the maker after its review, 29341fcf…; the router reads one new thing on its
#: own as `manage`, 25218696…; the maker makes its one thing once (E2's finish tools), 47fb48df…; what it made is
#: checked against what was approved, 7bb7c0fb…; SAI2-P3 (F942): the build route on the typed Plan — the planner and
#: the prompt writer in, the analyst, the agent creator and both savers out, with the four passages only they read,
#: 51bda0e8…; the planner's step allows 8 passes (five questions, a lookup and a re-plan after a connection),
#: 20cbae84…; the new agents' instructions are written before the review (Naveen, Q2 as revised on MCP2's review),
#: and SAI2's review of P3a: the lead-in follows the offers, the loop rule, a no re-reads the pack, Q&A kept.
#: SAI2's re-check: the review, the prompt writer, the pack and the answer steps end in words at their caps, 2deb6b5d…;
#: on F951's anchors, 83b25cbb…; a transform sets `result` and reaches no service itself (F947; SAI2's review, S6),
#: 63467c47…; SAI2-P2's F958: the change planner falls back to `cannot`, b5678c1d…; F960: what the person said is kept across the run (`change_heard`), 2f537dfd…;
#: P3b: the build's own researcher finishes on report_findings (E2), its shared passages in the library, 1ecd661d…; the
#: build makes a read API's tools after the yes and waits for their connection before the save (F944–F946), 1f82462e…;
#: SAI2-P4: the front door read in code — the turn, one typed classify call (its classifier agent), the answer's pack
#: built from what it needs, talk with no tools — and the reporter's one read tool per job (F756), 8d5a358b…;
#: SAI2's review of P4: the reporter's own prompt teaches no tool (each step's does), talk has room for the answer
#: turn, and the talker says only its role, c2b8ebd2….
#: P3c (F949): the wait waits for every way in, and a connection under other tool names re-plans, 452d13db….
#: F966: the planner says what each agent's model needs, never a model, a1823d76…; F967: a step that only calls a
#: tool needs no agent, and the steps passage teaches `config.tool_arguments`, f8874085…; F957: the steps passage
#: says what an expression may use from the engine's tables (`{{EXPRESSION_VOCABULARY}}`), 05a8a85d…; P3c:
#: `step_researched_before` goes, unreachable since P4's override sends findings and a yes to the build (F766), 5cbb0a48…;
#: SAI2-P5: the research route code first, its Researcher finishing on report_findings on what code left (its own
#: report line; the shared passages unchanged), the quick card step and its passage out; the Researcher looks up the providers it names itself in code
#: (`look_up_providers`), 0bd14cb9…; the route's own look-up is marked by its step (`by_route`, SAI2's review N1),
#: b9516051…; rebased on P3c (`step_researched_before` out), e3743489…; the build's look-up is code first (P3c's
#: splice of `look_up_providers`: the look-up, its route, and code's finals reported), d7e67981…; F1064: a bare
#: go-ahead after findings is shown to the planner as their goal (`plan_context.request`), 00723c8a…; the P5 gate:
#: the research route's Researcher holds only the web, the look-up and its report (its catalogue and Registry steps in
#: code), and the by-hand steps are a passage of the build's researcher's own (`research_by_hand`, its prompt
#: unchanged), 1d2a272d…; with F1121's question on what still can't be built (`step_plan_same`, and the `same` and
#: `no_tool` cases that reach it), 90b30a42…; with F1153's effort check after the role door (`step_effort`, its route
#: and `step_effort_offered`, with its four state keys and the press's route), 787b2662…; with F1193's Browser step
#: palette in the planner's prompt (`{state.browser_steps}` beside `knowledge_steps`, and its state key), 4acc59e4….
#: F1198: one new thing may be a Python tool (the change planner's `python_tool`), which the maker makes and proves,
#: finishing on its passing test, 4ddde80f…; with F1193's palette (fix-forward 13), 05eecc81….
#: F1219's turn limits
#: (Naveen's table with HOT3: 60 turns in 1200 s for the steps that read and plan, 40 in 600 s for the rest, the quick
#: classify step at 2; no prompt changes), 2fcbfc0f… alone; with F1193 and F1198 (fix-forward 13), ff332d9b….
#: F1228: the build's last message adds what each part does (`{state.build_saved.does}`), and the change planner's
#: rule 9 (never say what an existing workflow does), f86194bc….
#: F1229's `built` target (the classify step's words and the state key that carries the last build), bcf13e4a….
#: F1227's read_vault for a Transform (the step cheatsheet and plan_steps' transform line), 6dd7a340…; on fix-forward
#: 14's tree, d4772f96….
#: F1231: the change planner's rule 10 (a recording's request is the use_recording op) and `recordings` among its
#: read_workspace topics; the state key a name both a recording and a workflow is kept under, b266d656….
#: F1225: the planner's read_api_spec (the person's own API spec, among its tools; no prompt changes), 3d11ab71….
#: F1241: the router's route to a waiting plan's review (`step_review_research` among step_classify_route's cases; no
#: prompt changes), 16182ae3….
#: F1247: the two approval routers' keys declared as the bool their If/Else steps write, cbc03e50… alone; with F1241
#: (fix-forward 14), ab529469….
#: F1243: every answer goes through check_answer and is said by a print; the reporter's rule 4 never writes a plan or
#: asks for a yes, ed4e54e8… alone; with F1241 and F1247 (fix-forward 14), 148653dd….
#: F1244: the research rules' line naming a provider's own domains (`own_domains`, the ones it read), 7b900513… on its
#: branch; on fix-forward 14's tree, 4c222b83….
#: F1252: every template over state in the one form, `{{ state.x }}` (Naveen 10-30), c8d173a1….
#: F1256: the planner's rule 8 (what the pack's `not_here` lists is done on its screen, never built from workflow
#: parts; `cannot`, naming the screen), fb32ab63… alone; with F1252 (fix-forward 15), 34ca5faa….
#: F1253: the planner and the change planner ask every question at once in one form, a plan carries none, and an
#: address of theirs is never guessed (planner.json, change_planner.json, plan_steps' tool line); with F1252,
#: 6dd38c6f…; on fix-forward 15's tree with F1256, 5005cdff….
#: F1241's follow-up: while a plan waits, classify through `step_classify_door` to `step_classify_at_plan`, whose
#: classifier's one call requires `continues` and whose prompt names the plan and asks plainly; the classify step's
#: context and the classifier's kinds move into the library (`classify_context`, `classify_kinds`) unchanged,
#: c9ada080… on its branch; on fix-forward 15's tree, `classify_context` in the one form (F1252), 258533db….
#: The classify at a waiting plan names the plan in the one form, `{{ state.turn.plan_waiting }}` (F1252; it read the
#: same through the converter before), ac29e4a5….
#: F1255: classify's typed `feature` (evals; its line in the library's classify_kinds, D340's one field for every
#: feature), and the change planner's rule 11 (Evals' ops, an agent never scored alone) with topic `evals`, b5d2233c…
#: on its branch; on fix-forward 16's tree, 23bea1ae….
#: D340 D0: one planner makes and changes — the change planner and its step fold into `step_plan` (both toolsets, five
#: finishes with `say`, instructions in sections that keep each rule's number, the change planner's finish fallback),
#: gather runs both context readers for every act, and `step_plan_finish` routes each finish to the check that was
#: there (a question or a "can't" on a change to the change check), 2d1b2d39… on fix-forward 16's 45d6d0b3; with
#: F1255's rule 11 and topic `evals` under the one planner's # Changing, 20f244e4…; the change context's rounds
#: are the planner's (8, then the one 'built or changed' end), d3ac6152….
#: D340 D2: the planner researches while it plans — a `research_questions` finish (product, public, business) to
#: code's answers first (`step_research_answer`), the researcher for what is left (`step_research_find`, its answers
#: kept in code), the person in one form through `step_plan_finish`; the answers in its next pass's prompt, c7c55863…;
#: with the sites a message names in the pack, kept a conversation in `named_pages`, and the planner told the person's
#: word comes before what was read or found (Naveen 10-31), e4e744c8….
#: F1257: the plan researcher and the researcher read search_help, and research_rules says what this product does is
#: read there, never on the web; on fix-forward 15's 108bd3f3, 99033f20…; merged with v2.0 ce37b8be (the classify
#: at a waiting plan above), b0a8a77c….
#: F1257 on fix-forward 16's cdd314f4 (D0's one planner, D2's research): eac4c85c…; with the named pages above
#: (fix-forward 16), 9558556c….
#: F1265: a `cannot` names its `screen` (the row's id in not_here), whose link code adds: planner rule 8 and the
#: `cannot` line, a4b534bd… on its branch; with the named pages and F1257 (fix-forward 16), 4ebcf413….
#: F1258: search_knowledge and rule 12 (editing a knowledge base's note or text file), 77c5f233… on its branch;
#: on fix-forward 16's cdd314f4, under the one planner's # Changing as rule 12, 7f917720…; with the named
#: pages, F1257 and F1265 (fix-forward 16), 3c693816….
#: D340 D3: the planner's own check of a draft, `check_draft`, among its tools, 0733c955…; with the planner's
#: "# Structure" (a fetch or one known call is a Tool step, reshaping a transform or extract_json, an agent only for
#: judgement, a team or a browser agent for its kind of work, `check_draft` before submitting), 7417a4bf…; with F1258 and
#: F1265 (fix-forward 16), 113fa7e5….
#: F1262: the one planner's rule 13 under # Changing, after BRW1's 12 (a website widget is made or changed with the
#: widget_ ops, its visitor tools only those the person named), be1cb5b9… on fix-forward 15; on fix-forward 16's
#: ff605079, b67e5ccb….
#: F1263: rule 14 under # Changing (a chat channel is connected with channel_connect, its token typed on its card,
#: and changed with the channel_ ops), a9ef3db1… on fix-forward 15; on fix-forward 16's ff605079 with F1262, 25173721….
#: REACH1: rules 15–19 under # Changing (triage, savings, settings_set, a schedule's missed runs and target, variables
#: by name), rule 10's recordings as ops, topics run/variables/settings/savings in both lists; on REACH2's 0af7a248,
#: 5ad51eef…; with REACH2's topics widgets and channels in both lists (TRI1, 11-02), 963b1a71….
#: F1274: rule 20 under # Changing (asking an agent for the person is the op talk_to_agent, a card that names it, the
#: message and roughly what it costs, and shows its answer), 2516fd8f….
#: F1260 (a hosted workspace's): rule 21 (dead jobs retried or discarded, "who changed it", a support request) and
#: topics dead_jobs and audit in both lists, c6f1a12c….
#: Rule 21's support request: its severity asked with ask_person, its run only when it is about one (TRI1), 50276764….
#: F1296: the research ask's own route (`step_research_ask_route`), and an exit on every step of the planner's cycle
#: (`step_plan_rounds`' words before the yes, `step_connect_give_up` after), cef82d9e…; the new route on a canvas
#: spot of its own, beside the research ask (1300, 600), 69df0403….
#: F1295: a save refused after the yes goes back to the planner once (`step_save_route`, after `step_save`), da644451….
#: F1298 adds nothing here: the specs a conversation read are the engine's own key (`_specs_read`), undeclared.
#: F1288: # Building's rule 9 (a chart is a CSV in the Vault: follow the pack's `chart`, give the plan its
#: `vault_write`), 97b141a1… (computed by CI2 on 79e0a58b's tree).
#: F1302: # Structure's rules 1–2 are for a workflow's fixed steps; an agent people ask things of holds every tool and
#: knowledge base its questions need (an exception, the numbering unchanged), 1024bb50….
#: F1300: "# What you are given" names `signed_in_sites`, and # Structure's rule 1 says a site there is read by a browser
#: agent with that kept session (an exception, the numbering unchanged), 0c0cb79d….
#: F1308: rule 12 under # Changing splits a range or a list so the rest reads as it did (TRI1), 7888cfe6….
#: F1311: the help answer holds `read_workspace` and its prompt says when it reads, after the help, 2160ed28….
#: F1311 (TRI1): a question about using the product is answered from the help alone, one about the workspace's own
#: spending, runs or results reads it, 47b11d21….
#: F1310: # Changing rule 22 (a "don't know" is answered with a change to approve) and step_change_clarify, run out,
#: back to step_plan, 558f132c….
#: F1308: rule 12 under # Changing splits a range or a list so the rest reads as it did (TRI1), 7888cfe6….
#: F1322: the planner's rule 5 (an HTTP tool whose address it knows is made in the change, by its own fields), on
#: F1310 and F1308 together, e9b9dcbe….
BUNDLE_SHA256 = "8270965471e29f1ad5fccc52a381bd9880a040a253ce776108a77241fce9c1df"


def _sha(definitions) -> str:
    return hashlib.sha256(json.dumps(definitions, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class TestTheBundleIsWhatItWas:
    def test_byte_for_byte(self):
        assert _sha(seed.bundled_definitions()) == BUNDLE_SHA256
        assert seed.bundle_version() == BUNDLE_SHA256[:16]

    def test_no_token_is_left_unexpanded(self):
        text = json.dumps(seed.bundled_definitions(), ensure_ascii=False)
        assert "{{PROMPT:" not in text and "{{STEP_TYPE_CHEATSHEET}}" not in text

    def test_the_shared_passages_are_named_not_pasted(self):
        """The sources name their fragments; the fragments are the library's."""
        raw = prompt_lint.raw_places()
        named = {name for text in raw.values() for name in seed.FRAGMENT.findall(text)}
        assert named == set(seed.fragment_names()), "every fragment is used, and every one used exists"
        assert all(len(seed.prompt_fragment(n)) > 40 for n in named)


class TestAFragment:
    def test_an_unknown_name_is_an_error_not_a_blank(self):
        with pytest.raises(KeyError, match="no_such_passage"):
            seed.expand_prompts("Before. {{PROMPT:no_such_passage}} After.")

    def test_it_expands_where_it_is_named(self):
        # Every passage, with the engine's own tokens it names written in as `expand_prompts` writes them (F957's
        # `{{EXPRESSION_VOCABULARY}}` in plan_steps): the first passage alone stopped being a token-free one when P5
        # took findings_call_report_findings out.
        from core.expressions import vocabulary_sentence
        from core.native_builder import STEP_TYPE_CHEATSHEET

        for name in seed.fragment_names():
            passage = (seed.prompt_fragment(name).replace("{{STEP_TYPE_CHEATSHEET}}", STEP_TYPE_CHEATSHEET)
                       .replace("{{EXPRESSION_VOCABULARY}}", vocabulary_sentence()))
            assert seed.expand_prompts(f"A {{{{PROMPT:{name}}}}} B") == f"A {passage} B", name


class TestThePromptFormRules:
    def test_f757_no_marker_block(self):
        assert prompt_lint.marker_blocks(prompt_lint.expanded_places()) == []

    def test_f757_finds_one(self):
        assert prompt_lint.marker_blocks({"agent:x": "Write a [REASONING] block before every call."}) == [
            ("agent:x", "[REASONING]")]

    def test_f764_no_sentence_in_two_places(self):
        assert prompt_lint.duplicated_sentences(prompt_lint.raw_places(with_fragments=True)) == {}

    def test_f764_finds_one(self):
        twice = "Call the tool once for every provider the report below looked at, then stop and wait."
        found = prompt_lint.duplicated_sentences({"step:a": f"Hello. {twice}", "step:b": twice})
        assert list(found.values()) == [["step:a", "step:b"]]

    def test_f767_every_loop_leads_with_its_item(self):
        assert prompt_lint.loops_not_item_first(seed.bundled_definitions()["orchestration"]) == []

    def test_f767_finds_one(self):
        orch = {"steps": [
            {"id": "each", "type": "loop", "loop_over": "state.orders", "loop_item_key": "order",
             "loop_step_ids": ["ask", "say"]},
            {"id": "ask", "type": "agent", "prompt_template": "Status of the order?\n{state.order}"},
            {"id": "say", "type": "llm", "prompt_template": "Order {state.order}: write one line on its status."},
        ]}
        assert prompt_lint.loops_not_item_first(orch) == ["ask"]


class TestAPromptTeachesOnlyTheToolsItsAgentHolds:
    """F800 (SAI2-P2): the class behind F803. A model taught a tool it cannot call calls it: gpt-oss-120b called the
    `update_tasks` its prompt taught but its turn did not offer, 3 times in 12. A prompt an agent reads — its own and
    its steps' — names in backticks only tools that agent holds. The one today's bundle had (the analyst's "NEVER call
    `list_all_tools`", a tool it does not hold) is gone rather than listed."""

    def test_none_does(self):
        assert prompt_lint.tools_not_held() == []

    def test_one_that_does_is_named(self):
        agents = [{"id": "a", "tools": ["read_workspace"], "system_prompt": "Read with `read_workspace`, then call "
                                                                             "`delete_agent` and `update_tasks`."}]
        orch = {"steps": [{"id": "s", "agent_id": "a", "prompt_template": "Finish with `submit_change`."}]}
        assert prompt_lint.tools_not_held(agents, orch) == [("agent:a", "delete_agent"),
                                                            ("step:s.prompt_template", "submit_change")]

    def test_a_step_that_narrows_the_tools_narrows_what_may_be_taught(self):
        """SAI2's review 5 (SAI2-P4): a step's `allowed_tools` is what that step's turn holds. The agent's prompt, read
        on that step too, may teach only those; so may the step's own."""
        agents = [{"id": "a", "tools": ["read_workspace", "search_help"],
                   "system_prompt": "Answer from facts. Read with `read_workspace`."}]
        orch = {"steps": [
            {"id": "help", "agent_id": "a", "allowed_tools": ["search_help"],
             "prompt_template": "Use `search_help`, then `read_workspace`."},
            {"id": "all", "agent_id": "a", "prompt_template": "Use `read_workspace`."}]}
        assert prompt_lint.tools_not_held(agents, orch) == [("step:help.agent_prompt", "read_workspace"),
                                                            ("step:help.prompt_template", "read_workspace")]

    def test_a_name_that_is_no_tool_is_not_counted(self):
        agents = [{"id": "a", "tools": [], "system_prompt": "Use the op `edit_steps` and its `what` field."}]
        assert prompt_lint.tools_not_held(agents, {"steps": []}) == []
