from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from servicepilot.conversations.pending import ProposalDecision


class InterpretationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ProposalDecision
    condition: str | None = Field(default=None, max_length=500)
    clarification_sms: str | None = Field(
        default=None, max_length=320,
        description="A brief customer-facing reply only for clarify. Ask about the exact pending change "
        "or acknowledge that the customer needs time. Never claim the change was made.",
    )
    explicit_proposal_reference: bool = Field(
        default_factory=bool,
        description="True only when this SMS identifies the exact change in its own words, "
        "for example by identifying the proposed time, service or appointment. "
        "Bare contextual assent alone is never an explicit reference.",
    )


class ConfirmationAttempt(BaseModel):
    attempt: int
    proposal_id: UUID
    prompt_id: UUID
    interpretation: Literal[
        "not_run", "pending", "decided", "missing", "invalid", "duplicate", "tool_error", "refused", "recovered",
    ] = "not_run"
    decision: ProposalDecision | None = None
    tool_invocations: int = 0
    explicit_proposal_reference: bool = False
    authorization: Literal["accepted", "rejected"] | None = None
    action_outcome: str | None = None
    error_code: str | None = None

    def interpretation_error(self) -> str | None:
        if self.interpretation == "decided" and self.tool_invocations == 1 and self.decision is not None:
            return None
        if self.interpretation == "pending":
            self.interpretation = "missing"
        elif self.interpretation == "decided":
            self.interpretation = "invalid"
        self.decision = None
        return f"confirmation_{self.interpretation}"

    def record_outcome(self, outcome: str) -> None:
        self.action_outcome = outcome
        self.authorization = (
            "accepted" if outcome in {"succeeded", "declined", "revised", "confirmation_required"} else "rejected"
        )
