"""Same-UUID provisioning with serialized, irreversible deletion fences."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.user import User
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.site_identity_models import SiteIdentityFence
from smarter_dev.web.site_identity_models import SiteIdentityKey
from smarter_dev.web.site_identity_models import SiteIdentityState


class ProviderIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["discord", "github", "google"]
    subject: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    email_verified: bool = False

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, value: str) -> str:
        if not value.isascii() or not value.isdecimal():
            raise ValueError("Invalid provider subject")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        if value is not None and (
            "@" not in value or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("Invalid email")
        return value


class IdentitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1, le=9_007_199_254_740_991)
    identities: list[ProviderIdentity] = Field(max_length=20)
    display_name: str | None = Field(default=None, max_length=255)

    @field_validator("identities")
    @classmethod
    def unique_identities(
        cls, values: list[ProviderIdentity]
    ) -> list[ProviderIdentity]:
        keys = [(v.provider, v.subject) for v in values]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate identity")
        return sorted(values, key=lambda v: (v.provider, v.subject))


def fence_digest(user_id: UUID) -> str:
    key = get_settings().site_identity_fence_key
    if not key or len(key) < 32:
        raise plain_error(503, "Identity service unavailable")
    return hmac.new(
        key.encode(), b"site-identity-v1:" + str(user_id).encode(), hashlib.sha256
    ).hexdigest()


async def verify_identity_key(session: AsyncSession) -> None:
    """Pin/verify the operator key without storing an account identifier."""
    key = get_settings().site_identity_fence_key
    if not key or len(key) < 32:
        raise plain_error(503, "Identity service unavailable")
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    # Pin an independent verifier once. A misconfigured replica/key replacement
    # must not create a second, ineffective family of deletion fences.
    verifier = hmac.new(
        key.encode(),
        b"site-identity-key-v1",
        hashlib.sha256,
    ).hexdigest()
    pinned = await session.get(SiteIdentityKey, 1)
    if pinned is None:
        await session.execute(
            insert(SiteIdentityKey)
            .values(id=1, verifier=verifier)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        pinned = await session.get(SiteIdentityKey, 1, populate_existing=True)
    if not hmac.compare_digest(pinned.verifier, verifier):
        raise plain_error(503, "Identity service unavailable")


async def lock_identity(session: AsyncSession, user_id: UUID) -> SiteIdentityFence:
    digest = fence_digest(user_id)
    await verify_identity_key(session)
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    # INSERT arbitrates an absent key too. PostgreSQL waits on an uncommitted
    # conflicting insert; the subsequent row lock is held until commit/rollback.
    await session.execute(
        insert(SiteIdentityFence)
        .values(digest=digest, deleted=False)
        .on_conflict_do_nothing(index_elements=["digest"])
    )
    return await session.scalar(
        select(SiteIdentityFence)
        .where(SiteIdentityFence.digest == digest)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def fence_account_deletion(session: AsyncSession, user_id: UUID) -> None:
    fence = await lock_identity(session, user_id)
    fence.deleted = True
    await session.execute(
        delete(SiteIdentityState).where(SiteIdentityState.user_id == user_id)
    )
    await session.flush()


async def provision_identity(
    session: AsyncSession, user_id: UUID, snapshot: IdentitySnapshot
) -> tuple[bool, list[str]]:
    fence = await lock_identity(session, user_id)
    if fence.deleted:
        raise plain_error(410, "identity_disabled")
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is not None and not user.is_active:
        raise plain_error(410, "identity_disabled")
    state = await session.get(SiteIdentityState, user_id)
    fingerprint = hashlib.sha256(
        json.dumps(
            snapshot.model_dump(), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if state and snapshot.revision <= state.revision:
        if snapshot.revision != state.revision or fingerprint != state.fingerprint:
            raise plain_error(409, "stale_revision")
        # Do not recreate a user removed outside the handoff's deletion path.
        if user is None:
            raise plain_error(410, "identity_disabled")
    accounts = list(
        (
            await session.scalars(
                select(OAuthAccount).where(OAuthAccount.user_id == user_id)
            )
        ).all()
    )
    by_key = {(a.provider, a.provider_account_id): a for a in accounts}
    for identity in snapshot.identities:
        owner = await session.scalar(
            select(OAuthAccount.user_id).where(
                OAuthAccount.provider == identity.provider,
                OAuthAccount.provider_account_id == identity.subject,
            )
        )
        if owner is not None and owner != user_id:
            raise plain_error(409, "identity_conflict")
        if identity.email_verified and identity.email:
            email_owner = await session.scalar(
                select(User.id).where(User.email == identity.email)
            )
            if email_owner is not None and email_owner != user_id:
                raise plain_error(409, "identity_conflict")
    if state and snapshot.revision == state.revision:
        if any((i.provider, i.subject) not in by_key for i in snapshot.identities):
            raise plain_error(409, "identity_conflict")
        return False, []
    if (
        user
        and state is None
        and snapshot.identities
        and not any((i.provider, i.subject) in by_key for i in snapshot.identities)
    ):
        raise plain_error(409, "identity_conflict")
    created = user is None
    if created:
        if state or not snapshot.identities:
            raise plain_error(409, "identity_conflict")
        verified_email = next(
            (i.email for i in snapshot.identities if i.email_verified and i.email), None
        )
        user = User(
            id=user_id, email=verified_email, name=snapshot.display_name, is_active=True
        )
        session.add(user)
        await session.flush()
    # Cookie-only existing accounts need no provider assertion. Do not create
    # provenance that a later request could use to attach unrelated logins.
    if state is None and not snapshot.identities:
        return False, []
    owned = set(state.owned_accounts if state else [])
    wanted = {(i.provider, i.subject) for i in snapshot.identities}
    removed_discord: list[str] = []
    for account in accounts:
        if (
            str(account.id) in owned
            and (account.provider, account.provider_account_id) not in wanted
        ):
            if account.provider == "discord":
                removed_discord.append(account.provider_account_id)
            owned.remove(str(account.id))
            await session.delete(account)
    for identity in snapshot.identities:
        account = by_key.get((identity.provider, identity.subject))
        if account is None:
            account = OAuthAccount(
                user_id=user_id,
                provider=identity.provider,
                provider_account_id=identity.subject,
                provider_email=identity.email,
                provider_email_verified=identity.email_verified,
            )
            session.add(account)
            await session.flush()
            owned.add(str(account.id))
        elif str(account.id) in owned:
            account.provider_email = identity.email
            account.provider_email_verified = identity.email_verified
    if state is None:
        state = SiteIdentityState(user_id=user_id)
        session.add(state)
    state.revision = snapshot.revision
    state.fingerprint = fingerprint
    state.owned_accounts = sorted(owned)
    await session.flush()
    return created, removed_discord
