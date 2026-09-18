"""Command-line administration: create tenants and API keys.

Run with ``uv run ragbridge-admin <command>``. Minting credentials has to
happen somewhere; an HTTP endpoint that did it would itself need
authentication, bringing the bootstrap problem right back (decision 4,
docs/plans/phase-3.md). A command needs shell access to the server, a
much higher bar than an internet-facing endpoint.
"""

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.auth import api_key_prefix, generate_api_key, hash_api_key
from ragbridge.config import Settings
from ragbridge.db.models import ApiKey, Tenant
from ragbridge.db.session import create_engine, create_session_factory


def _print_new_key(key: str) -> None:
    print(f"API key: {key}")
    print("This key is shown once. Only its hash is stored - save it now.")


async def create_tenant(session_factory: async_sessionmaker[AsyncSession], name: str) -> None:
    """Create a tenant with one API key, and print the key once."""
    key = generate_api_key()
    async with session_factory() as session:
        tenant = Tenant(name=name)
        session.add(tenant)
        await session.flush()
        session.add(
            ApiKey(
                tenant_id=tenant.id,
                key_hash=hash_api_key(key),
                prefix=api_key_prefix(key),
                name="default",
            )
        )
        await session.commit()
        print(f"Tenant {tenant.id} ({name}) created.")
    _print_new_key(key)


async def list_tenants(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Print every tenant, oldest first."""
    async with session_factory() as session:
        tenants = (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()
    if not tenants:
        print("No tenants.")
        return
    for tenant in tenants:
        print(f"{tenant.id}  {tenant.name}  created {tenant.created_at}")


async def create_key(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, name: str
) -> None:
    """Create an additional API key for an existing tenant, and print it once."""
    key = generate_api_key()
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            print(f"No such tenant: {tenant_id}", file=sys.stderr)
            raise SystemExit(1)
        session.add(
            ApiKey(
                tenant_id=tenant_id,
                key_hash=hash_api_key(key),
                prefix=api_key_prefix(key),
                name=name,
            )
        )
        await session.commit()
    _print_new_key(key)


async def revoke_key(session_factory: async_sessionmaker[AsyncSession], prefix: str) -> None:
    """Revoke an API key by its prefix. Sets revoked_at; never deletes the row."""
    async with session_factory() as session:
        api_key = await session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
        if api_key is None:
            print(f"No such API key: {prefix}", file=sys.stderr)
            raise SystemExit(1)
        api_key.revoked_at = datetime.now(UTC)
        await session.commit()
    print(f"Revoked key {prefix}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_tenant_parser = subparsers.add_parser(
        "create-tenant", help="Create a tenant with one API key."
    )
    create_tenant_parser.add_argument("--name", required=True)

    subparsers.add_parser("list-tenants", help="List all tenants.")

    create_key_parser = subparsers.add_parser(
        "create-key", help="Create an additional API key for an existing tenant."
    )
    create_key_parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    create_key_parser.add_argument("--name", default="default")

    revoke_key_parser = subparsers.add_parser(
        "revoke-key", help="Revoke an API key by its prefix (see list-tenants)."
    )
    revoke_key_parser.add_argument("--prefix", required=True)

    args = parser.parse_args()
    session_factory = create_session_factory(create_engine(Settings()))

    if args.command == "create-tenant":
        asyncio.run(create_tenant(session_factory, args.name))
    elif args.command == "list-tenants":
        asyncio.run(list_tenants(session_factory))
    elif args.command == "create-key":
        asyncio.run(create_key(session_factory, args.tenant_id, args.name))
    elif args.command == "revoke-key":
        asyncio.run(revoke_key(session_factory, args.prefix))


if __name__ == "__main__":
    main()
