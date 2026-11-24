import pytest
from sqlalchemy import func, select

from servicepilot.db.models import Appointment, Customer, Tenant, Vehicle
from servicepilot.seed import create_simulation_customer, seed_database


async def test_seed_is_repeatable_and_creates_two_consistent_dealerships(session):
    await seed_database(session)
    await session.commit()
    await seed_database(session)
    await session.commit()

    tenants = (await session.scalars(select(Tenant).order_by(Tenant.slug))).all()
    assert [tenant.slug for tenant in tenants] == ["cedar-ridge-motors", "harbor-point-auto"]
    assert tenants[0].service_catalog["tire rotation"]["minutes"] == 30
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


async def test_demo_catalog_backfill_preserves_existing_knowledge_and_limits_scope(session):
    harbor = Tenant(slug="harbor-point-auto", name="An existing real tenant with a reserved slug")
    session.add(harbor)
    await session.flush()
    await seed_database(session)
    cedar = await session.scalar(select(Tenant).where(Tenant.slug == "cedar-ridge-motors"))
    cedar.service_catalog = {}
    cedar.service_policies = {"fee": "Call us"}
    await session.flush()
    await seed_database(session)
    assert cedar.service_catalog == {}
    await seed_database(session, backfill_service_catalog=True)
    for tenant in (cedar, harbor):
        await session.refresh(tenant)
    assert cedar.service_catalog["tire rotation"]["minutes"] == 30
    assert cedar.service_policies == {"fee": "Call us"}
    assert harbor.service_catalog == {}
    assert not await session.scalar(select(Customer.id).where(Customer.tenant_id == harbor.id))
    cedar.service_catalog = {}
    cedar.service_settings_managed = True
    await session.flush()
    await seed_database(session, backfill_service_catalog=True)
    assert cedar.service_catalog == {}


async def test_new_simulation_customer_is_scoped_and_has_no_appointment(session):
    await seed_database(session)
    with pytest.raises(ValueError, match="seeded demo dealerships"):
        await create_simulation_customer(session, "other-dealer")
    phones = [await create_simulation_customer(session, "cedar-ridge-motors") for _ in range(2)]
    assert len(set(phones)) == 2
    rows = (await session.execute(select(Customer, Tenant.slug).join(Tenant).where(Customer.phone_number.in_(phones)))).all()
    assert len(rows) == 2
    for customer, slug in rows:
        assert slug == "cedar-ridge-motors"
        assert await session.scalar(select(Vehicle.id).where(Vehicle.customer_id == customer.id))
        assert not await session.scalar(select(Appointment.id).where(Appointment.customer_id == customer.id))
