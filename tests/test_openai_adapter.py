import os
from unittest.mock import AsyncMock

import pytest
from google.adk.models.lite_llm import LiteLLMClient
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from litellm import ModelResponse

# These tests verify the OpenAI/LiteLLM adapter path specifically.
# They are skipped when the configured model is not an openai/ model.
openai_only = pytest.mark.skipif(
    not os.getenv("AGENT_MODEL", "gemini-2.0-flash").startswith("openai/"),
    reason="OpenAI adapter tests only run when AGENT_MODEL starts with 'openai/'",
)


@openai_only
@pytest.mark.asyncio
async def test_openai_tool_call_mapping_without_network(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "openai/gpt-6-astra")
    # Re-import to pick up the monkeypatched env
    import importlib
    import ranking_agent.agent as agent_mod
    importlib.reload(agent_mod)
    agent = agent_mod.root_agent

    response = ModelResponse(choices=[{
        "index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_test", "type": "function", "function": {
                "name": "get_ranking_check", "arguments": '{"job_id":"demo-job"}'
            }
        }]}
    }])
    transport = AsyncMock(return_value=response)
    monkeypatch.setattr(LiteLLMClient, "acompletion", transport)
    request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="Read demo-job")])])
    replies = [reply async for reply in agent.model.generate_content_async(request)]
    call = replies[-1].content.parts[0].function_call
    assert call.name == "get_ranking_check"
    assert call.args == {"job_id": "demo-job"}
    assert transport.call_args.kwargs["model"] == "openai/gpt-6-astra"
    assert transport.call_args.kwargs["api_base"] == "https://api.openai.com/v1"


@openai_only
@pytest.mark.asyncio
async def test_tool_result_is_sent_back_to_openai(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "openai/gpt-6-astra")
    import importlib
    import ranking_agent.agent as agent_mod
    importlib.reload(agent_mod)
    agent = agent_mod.root_agent

    transport = AsyncMock(return_value=ModelResponse(choices=[{
        "index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Check blocked; no rank."}
    }]))
    monkeypatch.setattr(LiteLLMClient, "acompletion", transport)
    request = LlmRequest(contents=[
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            id="call_test", name="get_ranking_check", args={"job_id": "demo-job"}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id="call_test", name="get_ranking_check", response={"status": "blocked", "organic_rank": None}))]),
    ])
    replies = [reply async for reply in agent.model.generate_content_async(request)]
    assert replies[-1].content.parts[0].text == "Check blocked; no rank."
    messages = transport.call_args.kwargs["messages"]
    assert any(m["role"] == "tool" and "blocked" in m["content"] for m in messages)

