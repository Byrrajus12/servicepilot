import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agents import OpenAIResponsesModel
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from sqlalchemy.ext.asyncio import async_sessionmaker

from servicepilot.agent.advisor import advisor_model_settings, run_advisor
from servicepilot.agent.confirmation import ConfirmationAttempt
from servicepilot.agent.tools import AdvisorContext
from servicepilot.config import Settings
from servicepilot.conversations.pending import PublishedProposal


def function_call(arguments, index=1):
    return ResponseFunctionToolCall(
        type="function_call", name="interpret_appointment_reply", arguments=arguments,
        call_id=f"call_{index}", id=f"fc_{index}", status="completed",
    )


def message(content):
    return ResponseOutputMessage(
        type="message", id="msg_1", role="assistant", status="completed", content=[content],
    )


@pytest.mark.parametrize(("output", "interpretation", "decision", "calls"), [
    ([function_call('{"decision":"confirm"}')], "decided", "confirm", 1),
    ([function_call('{"decision":"clarify"}')], "decided", "clarify", 1),
    ([function_call('{"decision":"clarify","clarification_sms":"Would you like me to cancel it?"}')],
     "decided", "clarify", 1),
    ([function_call('{"decision":"confirm","clarification_sms":"Would you like me to cancel it?"}')],
     "decided", "confirm", 1),
    ([function_call('{"decision":"unrelated"}')], "decided", "unrelated", 1),
    ([function_call('{"decision":"probably"}')], "invalid", None, 1),
    ([function_call('{"decision":')], "invalid", None, 1),
    ([function_call('{"decision":"confirm","extra":true}')], "invalid", None, 1),
    ([function_call('{"decision":"confirm"}'), function_call('{"decision":"decline"}', 2)], "duplicate", None, 2),
    ([message({"type": "output_text", "text": "All set", "annotations": []})], "missing", None, 0),
    ([message({"type": "refusal", "refusal": "I cannot answer"})], "refused", None, 0),
])
async def test_confirmation_contract_with_real_sdk_runner(monkeypatch, output, interpretation, decision, calls):
    pending = PublishedProposal(
        id=uuid4(), kind="cancel", customer_id=uuid4(), vehicle_id=uuid4(), appointment_id=uuid4(),
        vehicle_label="Ford Escape", dealership="Test dealer", timezone="UTC", service_type="Oil change",
        original_start=datetime(2027, 1, 4, 9, tzinfo=UTC), request_id=uuid4(), prompt_id=uuid4(),
        published_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    context = AdvisorContext(uuid4(), uuid4(), pending.customer_id, async_sessionmaker(), uuid4(), 1, pending=pending)
    diagnostic = ConfirmationAttempt(attempt=1, proposal_id=pending.id, prompt_id=pending.prompt_id, interpretation="pending")
    context.confirmation = diagnostic
    model_calls = 0

    async def model_response(_self, *, model_settings, tools, output_schema, **kwargs):
        nonlocal model_calls
        model_calls += 1
        assert model_settings.tool_choice == "interpret_appointment_reply"
        assert model_settings.parallel_tool_calls is False and model_settings.store is False
        assert [tool.name for tool in tools] == ["interpret_appointment_reply"]
        assert output_schema is None
        return ModelResponse(output=output, usage=Usage(requests=1), response_id="resp_1")

    monkeypatch.setattr(OpenAIResponsesModel, "get_response", model_response)
    settings = Settings(database_url="postgresql+asyncpg://localhost/unused", openai_api_key="test-key")
    reply = await run_advisor(settings, context, '{"current_inbound_sms":"test fixture"}')
    assert model_calls == reply.model_calls == 1 and reply.body is None
    assert (diagnostic.interpretation, diagnostic.decision, diagnostic.tool_invocations) == (
        interpretation, decision, calls,
    )
    expected_sms = json.loads(output[0].arguments).get("clarification_sms") if decision == "clarify" else None
    assert context.clarification_sms == expected_sms


async def test_advisor_preserves_composed_sms_through_real_sdk_runner(monkeypatch):
    sms = "Your oil change is Monday at 9. We also do tire rotations and brake inspections."
    context = AdvisorContext(uuid4(), uuid4(), None, async_sessionmaker(), uuid4(), 1)

    async def model_response(_self, *, output_schema, **kwargs):
        assert output_schema is not None
        output = message({"type": "output_text", "text": json.dumps({"sms": sms}), "annotations": []})
        return ModelResponse(output=[output], usage=Usage(requests=1), response_id="resp_1")

    monkeypatch.setattr(OpenAIResponsesModel, "get_response", model_response)
    settings = Settings(database_url="postgresql+asyncpg://localhost/unused", openai_api_key="test-key")
    reply = await run_advisor(settings, context, '{"current_inbound_sms":"Any appointments and what services do you do?"}')
    assert reply.body == sms


@pytest.mark.parametrize(("model", "requested", "expected", "tokens"), [
    ("gpt-4.1-mini", None, None, 512),
    ("gpt-6-luna", None, "low", 2048),
    ("gpt-6-luna", "none", "none", 512),
    ("gpt-6.1-terra", "low", "low", 2048),
])
def test_model_settings_reserve_reasoning_tokens_only_when_enabled(model, requested, expected, tokens):
    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://localhost/unused",
        agent_model=model, agent_reasoning_effort=requested,
    )
    result = advisor_model_settings(settings)
    assert (result.reasoning.effort if result.reasoning else None) == expected
    assert result.max_tokens == tokens and result.parallel_tool_calls is False and result.store is False


def test_default_advisor_configuration():
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://localhost/unused")
    assert settings.agent_model == "gpt-6-luna"
    assert advisor_model_settings(settings).reasoning.effort == "low"
    assert settings.agent_timeout_seconds == 45
