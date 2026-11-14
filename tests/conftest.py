from datetime import date

import pytest
from sqlalchemy import select

from leave_tracker.models import User, make_session_factory
from leave_tracker.seed import seed
from leave_tracker.service import LeaveService

TODAY = date(2026, 11, 16)        # a Monday; all tests use this fixed "today"


@pytest.fixture()
def db():
    Session = make_session_factory("sqlite:///:memory:")
    with Session() as s:
        seed(s, today=TODAY)
        yield s


@pytest.fixture()
def svc(db):
    """svc('ravi@kestrel.test') -> LeaveService logged in as that employee."""
    def make(email: str) -> LeaveService:
        user = db.scalar(select(User).where(User.email == email))
        return LeaveService(db, user, today=TODAY)
    return make
