from sqlalchemy import func, select

from servicepilot.db.models import Appointment, Customer, Tenant, Vehicle
from servicepilot.seed import seed_database


async def test_seed_is_repeatable_and_creates_two_consistent_dealerships(session):
    await seed_database(session)
    await session.commit()
    await seed_database(session)
    await session.commit()

    tenants = (await session.scalars(select(Tenant).order_by(Tenant.slug))).all()
    assert [tenant.slug for tenant in tenants] == ["cedar-ridge-motors", "harbor-point-auto"]
    for tenant in tenants:
        for model in (Customer, Vehicle, Appointment):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tenant.id)
            )
            assert count == 3

        visits = (await session.scalars(
            select(Appointment)
            .where(Appointment.tenant_id == tenant.id)
            .order_by(Appointment.external_dms_id)
        )).all()
        assert [visit.external_dms_id for visit in visits] == ["DEMO-001", "DEMO-002", "DEMO-003"]
        for visit in visits:
            customer = await session.get(Customer, visit.customer_id)
            vehicle = await session.get(Vehicle, visit.vehicle_id)
            assert visit.tenant_id == customer.tenant_id == vehicle.tenant_id
            assert visit.customer_id == vehicle.customer_id
            assert visit.scheduled_start < visit.scheduled_end
            assert visit.status == "scheduled"
