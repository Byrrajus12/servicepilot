import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.config import Settings
from servicepilot.db.models import Appointment, Customer, Tenant, Vehicle
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


async def seed_database(session: AsyncSession) -> None:
    for slug, name, visits in DEALERSHIPS:
        await session.execute(
            insert(Tenant).values(slug=slug, name=name).on_conflict_do_nothing(index_elements=["slug"])
        )
        tenant_id = (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()

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
                    scheduled_end=start + timedelta(minutes=45),
                    service_type=visit.service_type,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "external_dms_id"])
            )


async def main() -> None:
    engine = build_engine(Settings())
    try:
        async with build_session_factory(engine).begin() as session:
            await seed_database(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
