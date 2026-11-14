"""Database tables (SQLAlchemy 2.0 ORM)."""
from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint,
                        create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from leave_tracker.config import DB_URL

ROLES = ("PM", "DEV", "BA")
STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
ACTIVE_STATUSES = ("PENDING", "APPROVED")      # these reserve dates and balance


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(20), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("org_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    day: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(100))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(3))          # PM, DEV or BA
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    project: Mapped["Project"] = relationship(back_populates="members", foreign_keys=[project_id])


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(100))
    members: Mapped[list[User]] = relationship(back_populates="project", foreign_keys=[User.project_id])


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    leave_type: Mapped[str] = mapped_column(String(10))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)             # working days only
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    approver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision_note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class CoverAssignment(Base):
    """BA acts as PM for a project while the PM is on approved leave."""
    __tablename__ = "cover_assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    pm_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ba_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    leave_request_id: Mapped[int] = mapped_column(ForeignKey("leave_requests.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(30))          # cover, approval_needed, decision, cancelled
    message: Mapped[str] = mapped_column(String(500))
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def make_session_factory(url: str = DB_URL):
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
