"""Run an opt-in semantic smoke test against AGENT_MODEL with synthetic SMS and no database."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker

from servicepilot.agent.actions import confirmation_prompt
from servicepilot.agent.advisor import run_advisor
from servicepilot.agent.confirmation import ConfirmationAttempt
from servicepilot.agent.tools import AdvisorContext
from servicepilot.config import Settings
from servicepilot.conversations.pending import PublishedProposal

CASES = (
    ("informal agreement", "ye go ahead", "confirm"),
    ("terse agreement", "ya", "confirm"),
    ("enthusiastic agreement", "Yawww", "confirm"),
    ("new informal agreement", "yuppp works for me", "confirm"),
    ("uncertain agreement", "umm maybe, let me think", "clarify"),
    ("decline", "nah leave it where it is", "decline"),
    ("revision", "Actually make it Wednesday at 9.", "revise"),
    ("agreement with a change", "yes but can you do Wednesday at 9 instead", "revise"),
    ("conditional agreement", "go ahead if there isn't a fee", "conditional"),
    ("substantive question", "Do you charge for moving it?", "question"),
    ("unrelated question", "What other services yall got?", "unrelated"),
    ("superseded proposal", "yeah the first time you offered", "clarify"),
)


async def evaluate() -> bool:
    settings = Settings(
        database_url="postgresql+asyncpg://localhost/unused", sms_transport="simulation",
    )
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY in your environment or .env.")

    pending = PublishedProposal(
        id=uuid4(), kind="reschedule", customer_id=uuid4(), vehicle_id=uuid4(), appointment_id=uuid4(),
        vehicle_label="2022 Ford Escape", dealership="Cedar Ridge Motors", timezone="America/New_York",
        service_type="Oil change", original_start=datetime(2026, 11, 30, 13, tzinfo=UTC),
        scheduled_start=datetime(2026, 11, 24, 13, tzinfo=UTC), request_id=uuid4(), prompt_id=uuid4(),
        published_at=datetime(2026, 11, 23, 20, tzinfo=UTC),
    )
    active_question = confirmation_prompt(pending)
    older_question = confirmation_prompt(pending.model_copy(update={
        "scheduled_start": datetime(2026, 11, 25, 13, tzinfo=UTC),
    }))
    payload = {
        "active_confirmation_question": active_question,
        "active_proposal": pending.model_dump(mode="json", include={"kind", "scheduled_start", "service_type"}),
        "history": [
            {"direction": "outbound", "body": older_question},
            {"direction": "inbound", "body": "How about Tuesday at 8 instead?"},
            {"direction": "outbound", "body": active_question},
        ],
    }
    all_passed = True
    for label, sms, expected in CASES:
        diagnostic = ConfirmationAttempt(
            attempt=1, proposal_id=pending.id, prompt_id=pending.prompt_id, interpretation="pending",
        )
        context = AdvisorContext(
            uuid4(), uuid4(), pending.customer_id, async_sessionmaker(), uuid4(), 1,
            pending=pending, confirmation=diagnostic,
        )
        await run_advisor(settings, context, json.dumps({**payload, "current_inbound_sms": sms}))
        passed = diagnostic.interpretation == "decided" and diagnostic.tool_invocations == 1 and (
            diagnostic.decision == expected
        )
        if expected == "confirm":
            passed &= not diagnostic.explicit_proposal_reference
        all_passed &= passed
        print(f"{'PASS' if passed else 'FAIL'} {label} (expected {expected}, got "
              f"{diagnostic.decision or diagnostic.interpretation})")
    return all_passed


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(evaluate()) else 1)
