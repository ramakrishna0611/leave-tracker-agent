"""Two-step login: company code + company password, then employee email + password."""
import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from leave_tracker.models import Organization, User


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


class AuthError(Exception):
    pass


def login_organization(db: Session, code: str, password: str) -> Organization:
    org = db.scalar(select(Organization).where(Organization.code == code.strip().upper()))
    # Same message for "no such company" and "wrong password" so attackers cannot probe codes
    if org is None or not check_password(password, org.password_hash):
        raise AuthError("Invalid company code or password.")
    return org


def login_user(db: Session, org: Organization, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.org_id == org.id, User.email == email.strip().lower()))
    if user is None or not check_password(password, user.password_hash):
        raise AuthError("Invalid email or password.")
    return user
