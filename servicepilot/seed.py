import asyncio
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.config import Settings
from servicepilot.db.models import Appointment, Conversation, Customer, Tenant, Vehicle
from servicepilot.db.session import build_engine, build_session_factory


@dataclass(frozen=True)
class ServiceVisit:
    customer_name: str
    phone_number: str
    vin: str
    make: str
    model: str
    model_year: int
    service_type: str


DEALERSHIPS = (
    (
        "cedar-ridge-motors",
        "Cedar Ridge Motors",
        (
            ServiceVisit("Maya Chen", "+12065550101", "SPDEMO00000000001", "Ford", "Escape", 2022, "Oil change"),
            ServiceVisit("Jordan Reed", "+12065550102", "SPDEMO00000000002", "Ford", "Ranger", 2023, "Brake inspection"),
            ServiceVisit("Elena Ortiz", "+12065550103", "SPDEMO00000000003", "Ford", "Bronco Sport", 2024, "Tire rotation"),
        ),
    ),
    (
        "harbor-point-auto",
        "Harbor Point Auto",
        (
            ServiceVisit("Avery Green", "+13135550101", "SPDEMO00000000001", "Toyota", "Camry", 2023, "Battery check"),
            ServiceVisit("Samira Patel", "+13135550102", "SPDEMO00000000004", "Toyota", "RAV4", 2024, "Oil change"),
            ServiceVisit("Miles Baker", "+13135550103", "SPDEMO00000000005", "Toyota", "Corolla", 2022, "Wheel alignment"),
        ),
    ),
)

DEMO_SERVICES = {
    "oil change": {"name": "Oil change", "description": "Replace the engine oil and filter.", "minutes": 30},
    "brake inspection": {"name": "Brake inspection", "description": "Inspect the pads, rotors and brakes.", "minutes": 45},
    "tire rotation": {"name": "Tire rotation", "description": "Rotate the tires and check pressures.", "minutes": 30},
    "battery check": {"name": "Battery check", "description": "Test the battery and charging system.", "minutes": 30},
    "wheel alignment": {"name": "Wheel alignment", "description": "Check and adjust wheel alignment.", "minutes": 60},
    "multi-point inspection": {
        "name": "Multi-point inspection", "description": "Check tires, fluids, lights and visible wear.", "minutes": 45,
    },
}
DEMO_POLICIES = {"arrival": "Please check in at the service desk with your vehicle key."}
SIMULATION_CUSTOMER_PREFIX = "+142555501"


async def seed_database(session: AsyncSession, *, backfill_service_catalog: bool = False) -> None:
    for slug, name, visits in DEALERSHIPS:
        catalog = {key: value for key, value in DEMO_SERVICES.items()}
        inserted = await session.scalar(
            insert(Tenant).values(slug=slug, name=name, service_catalog=catalog, service_policies=DEMO_POLICIES)
            .on_conflict_do_nothing(index_elements=["slug"]).returning(Tenant.id)
        )
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug).with_for_update())
        tenant_id = tenant.id
        if inserted is None:
            demo_customer = uuid5(NAMESPACE_URL, f"servicepilot/demo/{tenant_id}/customer/1")
            # Match the existing fixture's deterministic identity and synthetic phone, not its slug alone.
            existing_demo = await session.scalar(select(Customer.id).where(
                Customer.id == demo_customer, Customer.tenant_id == tenant_id, Customer.phone_number == visits[0].phone_number,
            ))
            if existing_demo is None:
                continue
            if backfill_service_catalog and not tenant.service_settings_managed and not tenant.service_catalog:
                tenant.service_catalog = catalog

        for index, visit in enumerate(visits, start=1):
            customer_id = uuid5(NAMESPACE_URL, f"servicepilot/demo/{tenant_id}/customer/{index}")
            vehicle_id = uuid5(NAMESPACE_URL, f"servicepilot/demo/{tenant_id}/vehicle/{index}")
            start = datetime(2030, 1, 7, 15, tzinfo=UTC) + timedelta(hours=index - 1)
            await session.execute(
                insert(Customer)
                .values(id=customer_id, tenant_id=tenant_id, name=visit.customer_name, phone_number=visit.phone_number)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(
                insert(Vehicle)
                .values(
                    id=vehicle_id,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    vin=visit.vin,
                    make=visit.make,
                    model=visit.model,
                    model_year=visit.model_year,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(
                insert(Appointment)
                .values(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    vehicle_id=vehicle_id,
                    external_dms_id=f"DEMO-{index:03d}",
                    scheduled_start=start,
                    scheduled_end=start + timedelta(minutes=DEMO_SERVICES[visit.service_type.casefold()]["minutes"]),
                    service_type=visit.service_type,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "external_dms_id"])
            )


async def create_simulation_customer(session: AsyncSession, slug: str) -> str:
    demo = next((visits[0] for demo_slug, _, visits in DEALERSHIPS if slug == demo_slug), None)
    if demo is None:
        raise ValueError("Choose one of the seeded demo dealerships.")
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug).with_for_update())
    if tenant is None or not await session.scalar(select(Customer.id).where(
        Customer.tenant_id == tenant.id, Customer.phone_number == demo.phone_number,
        Customer.id == uuid5(NAMESPACE_URL, f"servicepilot/demo/{tenant.id}/customer/1"),
    )):
        raise ValueError("Seed the demo dealership before creating a simulation customer.")

    used = set(await session.scalars(select(Customer.phone_number).where(
        Customer.tenant_id == tenant.id, Customer.phone_number.startswith(SIMULATION_CUSTOMER_PREFIX),
    ).union(select(Conversation.sender_number).where(
        Conversation.tenant_id == tenant.id, Conversation.sender_number.startswith(SIMULATION_CUSTOMER_PREFIX),
    ))))
    for index in range(100):
        phone = f"{SIMULATION_CUSTOMER_PREFIX}{index:02}"
        if phone not in used:
            break
    else:
        raise ValueError("All reserved simulation customer numbers are in use for this dealership.")

    customer_id = uuid4()
    await session.execute(insert(Customer).values(
        id=customer_id, tenant_id=tenant.id, name=f"Simulation Customer {index:02}", phone_number=phone,
    ))
    await session.execute(insert(Vehicle).values(
        tenant_id=tenant.id, customer_id=customer_id, vin=f"SPSIM{customer_id.hex[:12].upper()}",
        make=demo.make, model=demo.model, model_year=demo.model_year,
    ))
    return phone


async def main() -> None:
    parser = ArgumentParser(description="Seed the fictional ServicePilot dealerships.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--backfill-service-catalog", action="store_true",
                       help="Fill empty service catalogs for the existing demo dealerships.")
    modes.add_argument("--new-simulation-customer", metavar="DEALERSHIP",
                       help="Create a fresh fictional customer and vehicle for local SMS simulation.")
    args = parser.parse_args()
    settings = Settings()
    if args.new_simulation_customer and (not settings.enable_sms_simulation or settings.sms_transport != "simulation"):
        parser.error("Creating a simulation customer requires enabled SMS simulation and simulation transport.")
    engine = build_engine(settings)
    try:
        phone = None
        async with build_session_factory(engine).begin() as session:
            if args.new_simulation_customer:
                phone = await create_simulation_customer(session, args.new_simulation_customer)
            else:
                await seed_database(session, backfill_service_catalog=args.backfill_service_catalog)
        if phone is not None:
            print(phone)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
