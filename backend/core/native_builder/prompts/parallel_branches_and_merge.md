## parallel — run branches at the same time, then join at a merge (no LLM of its own)
A branch is written as its entry step id. The engine follows that step's `next_step_id` chain until it reaches the parallel's OWN `next_step_id` — the join — so:
- `next_step_id` on a parallel step is REQUIRED and MUST be a `merge` step. Add one if the plan has none. Left empty, every branch runs the whole rest of the orchestration, one branch after another, and the first to reach `end` takes the run with it.
- Each branch's last step points at that same merge step.
- The merge step's own `next_step_id` continues the flow (review, print, end).
Example: `add_single_step(orch_id="orch_123", step_id="step_par1111", name="Analyse", type="parallel", parallel_branches_json="[[\"step_sum2222\"],[\"step_ent3333\"]]", next_step_id="step_mrg4444")` — with `step_sum2222` and `step_ent3333` both `next_step_id="step_mrg4444"`, and `step_mrg4444` a `merge` step.
