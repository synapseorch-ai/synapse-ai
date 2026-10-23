"""OSS's own Bedrock provider sends no native tools, so no tool name can trip Nova's name rule (F1011, Naveen
2026-10-24: a pin, not aliasing nothing calls).

HOT1's F878 (cloud, 2026-10-24): Nova Pro fails mid-stream ("Model produced invalid sequence as part of ToolUse")
when a tool it calls has a name outside [A-Za-z0-9_], as external MCP tools do (`google-workspace__…`). That is
cloud's gateway, which hands Converse a `toolConfig`; HOT1 aliases the names there. This provider never does: the
engine writes a Bedrock agent's tools into its system prompt as text and parses the calls itself
(`react_engine.inject_tools_in_prompt`), and history is text. This pins that. **Native Bedrock tools added here
later must alias their names** — HOT1's `tool_aliases` / `original_names` in cloud's `api/gateway/bedrock.py`, the
same function in both places so a name aliases alike.
"""
import core.llm_providers as llm

HYPHENATED = [{"type": "function", "function": {
    "name": "google-workspace__sheets_delete_rows", "description": "Delete rows.",
    "parameters": {"type": "object", "properties": {"sheet": {"type": "string"}}}}}]


async def test_a_bedrock_call_with_tools_sends_no_tool_config(monkeypatch):
    asked: list[dict] = []

    class _FakeBedrock:
        def converse(self, **kwargs):
            asked.append(kwargs)
            return {"output": {"message": {"content": [{"text": "ok"}]}},
                    "usage": {"inputTokens": 3, "outputTokens": 1}}

        def converse_stream(self, **kwargs):
            asked.append(kwargs)
            return {"stream": [{"contentBlockDelta": {"delta": {"text": "ok"}}},
                               {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 1}}}]}

    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
    settings = {"aws_access_key_id": "AKIA", "aws_secret_access_key": "s", "aws_region": "us-east-1"}
    # `_ex`: the suite's fake model stands in for `generate_response`.
    answer = await llm.generate_response_ex("Delete the empty rows.", "sys", "bedrock", "bedrock.amazon.nova-pro-v1:0",
                                            settings, tools=HYPHENATED)
    assert answer.text == "ok"
    assert asked, "Converse was called"
    assert not [k for k in asked if "toolConfig" in k], "no native tools: names never reach Converse's rule"
