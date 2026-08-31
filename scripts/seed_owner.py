import asyncio
import argparse
import sys
import uuid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.models.base import async_session
from app.core.models.user import ROLE_SOURCE_DEFAULT, User
from app.core.models.role import Role
from app.core.settings import settings


async def seed_owner(email: str):
    async with async_session() as session:
        # 1. Resolve the default role
        role_stmt = select(Role).where(Role.name == settings.DEFAULT_ROLE)
        role_result = await session.execute(role_stmt)
        role = role_result.scalar_one_or_none()

        if not role:
            print(f"Error: Default role '{settings.DEFAULT_ROLE}' not found in database.")
            sys.exit(1)

        # 2. Check if user exists
        user_stmt = select(User).where(User.email == email)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        try:
            if not user:
                # Create new user as owner
                print(f"Creating new user {email} as Owner...")
                user = User(
                    email=email,
                    keycloak_id=str(uuid.uuid4()),
                    role_id=role.id,
                    is_admin=True,
                    is_owner=True,
                    role_source=ROLE_SOURCE_DEFAULT,
                )
                session.add(user)
            else:
                # Promote existing user to owner
                print(f"Promoting existing user {email} to Owner...")
                user.is_admin = True
                user.is_owner = True

            await session.commit()
            print(f"Successfully seeded owner: {email}")

        except IntegrityError as e:
            await session.rollback()
            if "idx_single_owner" in str(e):
                print("Error: Another owner already exists. Use a different email or manually remove the existing owner.")
                sys.exit(1)
            else:
                print(f"Database integrity error: {e}")
                sys.exit(1)
        except Exception as e:
            await session.rollback()
            print(f"Unexpected error: {e}")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Seed the system Owner account.")
    parser.add_argument("--email", required=True, help="Email of the user to promote to Owner.")
    args = parser.parse_args()

    asyncio.run(seed_owner(args.email))


if __name__ == "__main__":
    main()
