from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from servicepilot.conversations.pending import ProposalDecision


class InterpretationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ProposalDecision


class ConfirmationAttempt(BaseModel):
    attempt: int
    proposal_id: UUID
    prompt_id: UUID
    interpretation: Literal[
        "not_run", "pending", "decided", "missing", "invalid", "duplicate", "tool_error", "refused", "recovered",
    ] = "not_run"
    decision: ProposalDecision | None = None
    tool_invocations: int = 0
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
