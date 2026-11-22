from fastapi import APIRouter, HTTPException, Request

from servicepilot.conversations.schemas import InboundResult, InboundSMS, SMSContent
from servicepilot.conversations.service import persist_inbound
from servicepilot.tenancy.auth import DealershipTransaction

router = APIRouter()
SIMULATED_RECIPIENT = "+12065550100"


@router.post("/simulations/sms")
async def simulate_sms(
    sms: SMSContent, dealership: DealershipTransaction, request: Request,
) -> InboundResult:
    if not request.app.state.settings.enable_sms_simulation:
        raise HTTPException(404, "SMS simulation is disabled")
    session, tenant_id = dealership
    return await persist_inbound(
        session, tenant_id,
        InboundSMS(**sms.model_dump(), provider="simulation", recipient_phone=SIMULATED_RECIPIENT),
    )
