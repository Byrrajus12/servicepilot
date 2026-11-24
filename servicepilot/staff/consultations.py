from sqlalchemy import select

from servicepilot.conversations.models import Conversation
from servicepilot.staff.models import Consultation


def thread_consultations(conversation: Conversation):
    return (
        Consultation.tenant_id == conversation.tenant_id,
        Consultation.conversation_id == conversation.id,
    )
