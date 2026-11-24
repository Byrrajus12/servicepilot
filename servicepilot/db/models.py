"""Load all model declarations for Alembic's metadata discovery."""

from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.models import Appointment
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import AppointmentActionResult
from servicepilot.customers.models import Customer
from servicepilot.integrations.models import IntegrationEvent
from servicepilot.sms.models import SMSConsent, SMSDelivery
from servicepilot.staff.models import Consultation, Employee, EmployeeSession, OwnershipEvent, StaffNote
from servicepilot.tenancy.models import Tenant
from servicepilot.vehicles.models import Vehicle
from servicepilot.workflows.models import Workflow

__all__ = [
    "AgentExecution",
    "Appointment",
    "AppointmentActionResult",
    "Conversation",
    "Consultation",
    "Customer",
    "Employee",
    "EmployeeSession",
    "IntegrationEvent",
    "Message",
    "OwnershipEvent",
    "SMSConsent",
    "SMSDelivery",
    "StaffNote",
    "Tenant",
    "Vehicle",
    "Workflow",
]
