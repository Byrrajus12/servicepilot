"""Load all model declarations for Alembic's metadata discovery."""

from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.models import Appointment
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import AppointmentActionResult
from servicepilot.customers.models import Customer
from servicepilot.integrations.models import IntegrationEvent
from servicepilot.tenancy.models import Tenant
from servicepilot.vehicles.models import Vehicle
from servicepilot.workflows.models import Workflow

__all__ = [
    "AgentExecution",
    "Appointment",
    "AppointmentActionResult",
    "Conversation",
    "Customer",
    "IntegrationEvent",
    "Message",
    "Tenant",
    "Vehicle",
    "Workflow",
]
