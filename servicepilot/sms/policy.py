from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.config import Settings
from servicepilot.sms.models import SMSConsent
from servicepilot.tenancy.models import Tenant


def twilio_channel(account_sid: str) -> str:
    return f"twilio-{account_sid.lower()}"


async def twilio_route_matches(
    session: AsyncSession, settings: Settings, tenant_id: UUID, channel: str, line: str, sender: str,
) -> bool:
    if settings.sms_transport != "twilio" or channel != twilio_channel(settings.twilio_account_sid):
        return False
    slug = settings.twilio_number_routes.get(line)
    if not slug or sender in settings.twilio_number_routes:
        return False
    return await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id, Tenant.slug == slug)) is not None


async def consent_for(
    session: AsyncSession, tenant_id: UUID, channel: str, sender: str, recipient: str,
) -> SMSConsent | None:
    return await session.scalar(select(SMSConsent).where(
        SMSConsent.tenant_id == tenant_id, SMSConsent.channel == channel,
        SMSConsent.sender == sender, SMSConsent.recipient == recipient,
    ))


def is_suppressed(consent: SMSConsent | None, since: datetime) -> bool:
    return consent is not None and (
        consent.suppressed or (consent.last_opt_out_at is not None and consent.last_opt_out_at >= since)
    )
