"""Usage says which models have no price, rather than showing them as free (U62).

A model missing from the rate card is priced $0 — on purpose since F63: a
guess from another family is a wrong number on a usage screen and a cost cap.
But nothing said so. `gemini-3.8-flash` read "$0.00" beside priced models, the
same as a free local one, and Home's "Spend by model" dropped it to the bottom.

Now each model row says whether the rate card has it (`priced`), read against
the card as it stands, so adding a price clears the mark.
"""
from core import usage_tracker


async def _log(model, provider):
    await usage_tracker.log_usage(model=model, provider=provider, input_tokens=1000, output_tokens=500,
                                  context_chars=100, session_id="s_u62", source="chat")


async def test_each_model_row_says_whether_it_has_a_price(client):
    await usage_tracker.seed_pricing_table()
    await _log("gpt-4o", "openai")
    await _log("gemini-3.8-flash", "gemini")
    await _log("qwen3:8b", "ollama")

    rows = {r["model"]: r for r in (await client.get("/api/usage/summary")).json()["by_model"]}

    assert rows["gpt-4o"]["priced"] is True and rows["gpt-4o"]["estimated_cost"] > 0
    assert rows["gemini-3.8-flash"]["priced"] is False and rows["gemini-3.8-flash"]["estimated_cost"] == 0
    assert rows["qwen3:8b"]["priced"] is False


async def test_adding_a_price_clears_the_mark(client):
    await usage_tracker.seed_pricing_table()
    await _log("gemini-3.8-flash", "gemini")
    table = await usage_tracker.get_pricing_table()
    await client.put("/api/usage/pricing", json={
        **table, "gemini-3.8-flash": {"provider": "gemini", "input_per_1m": 0.5, "output_per_1m": 3}})

    [row] = [r for r in (await client.get("/api/usage/summary")).json()["by_model"]
             if r["model"] == "gemini-3.8-flash"]
    assert row["priced"] is True
