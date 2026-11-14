"""Demo data: 2 companies, 3 projects each, every project has 1 PM, 2 developers and 1 BA.

Run:  python -m leave_tracker.seed          (rebuilds leave_tracker.db)
All demo passwords are listed in the README; they are for local testing only.
"""
from datetime import date, timedelta

from sqlalchemy.orm import Session

from leave_tracker.auth import hash_password
from leave_tracker.models import Base, Holiday, LeaveRequest, Organization, Project, User, make_session_factory

ORGS = [
    {"name": "Kestrel Systems", "code": "KSTL01", "password": "kestrel@2026", "domain": "kestrel.test",
     "projects": {
         "Phoenix CRM":     [("Anita Rao", "PM"), ("Ravi Kumar", "DEV"), ("Kiran Reddy", "DEV"), ("Meera Shah", "BA")],
         "Atlas Payments":  [("Vikram Singh", "PM"), ("Sneha Iyer", "DEV"), ("Arjun Nair", "DEV"), ("Pooja Das", "BA")],
         "Orion Analytics": [("Farah Khan", "PM"), ("Rahul Verma", "DEV"), ("Divya Menon", "DEV"), ("Nikhil Joshi", "BA")],
     },
     "holidays": [(date(2026, 11, 8), "Diwali"), (date(2026, 12, 25), "Christmas"), (date(2027, 1, 1), "New Year"),
                  (date(2027, 1, 14), "Sankranti"), (date(2027, 1, 26), "Republic Day")]},
    {"name": "Saffron Cloudworks", "code": "SAFR02", "password": "saffron@2026", "domain": "saffron.test",
     "projects": {
         "Helios App":   [("Sara Thomas", "PM"), ("Aditya Rao", "DEV"), ("Lakshmi Pillai", "DEV"), ("Omar Shaikh", "BA")],
         "Vega Data":    [("Rohan Gupta", "PM"), ("Isha Kapoor", "DEV"), ("Manoj Patil", "DEV"), ("Tanvi Kulkarni", "BA")],
         "Lyra Support": [("Priya Sharma", "PM"), ("Deepak Yadav", "DEV"), ("Anjali Bose", "DEV"), ("Karthik S", "BA")],
     },
     "holidays": [(date(2026, 11, 8), "Diwali"), (date(2026, 12, 25), "Christmas"), (date(2027, 1, 1), "New Year"),
                  (date(2027, 1, 26), "Republic Day")]},
]
USER_PASSWORD = "Welcome@123"   # same demo password for every employee


def email_for(name: str, domain: str) -> str:
    return name.split()[0].lower() + "@" + domain


def next_weekday(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)      # today if it already is that weekday


def seed(db: Session, today: date | None = None) -> None:
    today = today or date.today()
    pw_hash = hash_password(USER_PASSWORD)           # hash once: bcrypt is deliberately slow
    for spec in ORGS:
        org = Organization(name=spec["name"], code=spec["code"], password_hash=hash_password(spec["password"]))
        db.add(org)
        db.flush()
        for d, n in spec["holidays"]:
            db.add(Holiday(org_id=org.id, day=d, name=n))
        for pname, people in spec["projects"].items():
            proj = Project(org_id=org.id, name=pname)
            db.add(proj)
            db.flush()
            for name, role in people:
                db.add(User(org_id=org.id, name=name, email=email_for(name, spec["domain"]),
                            password_hash=pw_hash, role=role, project_id=proj.id))
        db.flush()

    # A few existing leaves so calendars and dashboards are not empty (relative to today)
    mon = next_weekday(today, 0)
    def leave(email, kind, start, end, days, status, reason):
        u = db.query(User).filter_by(email=email).one()
        db.add(LeaveRequest(org_id=u.org_id, user_id=u.id, leave_type=kind, start_date=start, end_date=end,
                            days=days, status=status, reason=reason))
    leave("ravi@kestrel.test", "casual", mon + timedelta(days=7), mon + timedelta(days=9), 3, "APPROVED", "Family function")
    leave("sneha@kestrel.test", "sick", mon, mon + timedelta(days=1), 2, "APPROVED", "Fever")
    leave("arjun@kestrel.test", "earned", mon + timedelta(days=14), mon + timedelta(days=18), 5, "PENDING", "Vacation")
    leave("rahul@kestrel.test", "casual", mon + timedelta(days=2), mon + timedelta(days=2), 1, "PENDING", "Bank work")
    leave("aditya@saffron.test", "casual", mon + timedelta(days=7), mon + timedelta(days=8), 2, "APPROVED", "Travel")
    db.commit()


def main() -> None:
    from leave_tracker.config import DB_URL
    from sqlalchemy import create_engine
    engine = create_engine(DB_URL)
    Base.metadata.drop_all(engine)
    Session = make_session_factory(DB_URL)
    with Session() as db:
        seed(db)
    print("Seeded 2 companies, 6 projects, 24 employees. Company passwords: KSTL01 / kestrel@2026, "
          f"SAFR02 / saffron@2026; every employee: {USER_PASSWORD}")


if __name__ == "__main__":
    main()
