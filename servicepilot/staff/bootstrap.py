"""Provision a dealership manager using direct database access."""

import argparse
import asyncio
from getpass import getpass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from servicepilot.config import Settings
from servicepilot.db.session import build_engine, build_session_factory
from servicepilot.staff.auth import hash_password
from servicepilot.staff.models import Employee
from servicepilot.staff.routes import CreateEmployee
from servicepilot.tenancy.models import Tenant


async def provision(dealership: str, account: CreateEmployee) -> None:
    password_hash = hash_password(account.password.get_secret_value())
    engine = build_engine(Settings())
    try:
        async with build_session_factory(engine).begin() as session:
            tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == dealership))
            if tenant_id is None:
                raise SystemExit("Dealership not found")
            employee_id = await session.scalar(insert(Employee).values(
                tenant_id=tenant_id, username=account.username, display_name=account.display_name,
                role="manager", password_hash=password_hash,
            ).on_conflict_do_nothing(index_elements=["tenant_id", "username"]).returning(Employee.id))
            if employee_id is None:
                raise SystemExit("That employee account already exists")
    finally:
        await engine.dispose()
    print(f"Created manager {account.username} for {dealership}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dealership")
    parser.add_argument("username")
    parser.add_argument("display_name")
    args = parser.parse_args()
    password = getpass("Password (at least 12 characters): ")
    if password != getpass("Repeat password: "):
        raise SystemExit("Passwords do not match")
    try:
        account = CreateEmployee(
            username=args.username, display_name=args.display_name, password=password, role="manager",
        )
    except ValidationError:
        raise SystemExit("Invalid username, display name, or password length") from None
    asyncio.run(provision(args.dealership, account))


if __name__ == "__main__":
    main()
