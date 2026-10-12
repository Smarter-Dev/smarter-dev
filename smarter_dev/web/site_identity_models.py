"""Identity handoff state; only the keyed fence survives account deletion."""

from uuid import UUID

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from smarter_dev.shared.database import Base


class SiteIdentityFence(Base):
    __tablename__ = "site_identity_fences"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SiteIdentityState(Base):
    __tablename__ = "site_identity_states"
    # Logical reference: Skrift owns User in separate SQLAlchemy metadata.
    # Account deletion removes this row in the same transaction as deactivation.
    user_id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    owned_accounts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class SiteIdentityKey(Base):
    __tablename__ = "site_identity_key"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    verifier: Mapped[str] = mapped_column(String(64), nullable=False)
