from datetime import date
from sqlalchemy import select
from leave_tracker.models import User, LeaveRequest, make_session_factory
from leave_tracker.seed import seed
from leave_tracker.service import LeaveService
from leave_tracker.agent import LeaveAgent
TODAY = date(2026, 11, 16)
db = make_session_factory("sqlite:///:memory:")(); seed(db, today=TODAY)
def run(email, turns):
    u = db.scalar(select(User).where(User.email == email))
    a = LeaveAgent(LeaveService(db, u, today=TODAY))
    print(f"\n===== {u.name} ({u.role})")
    for t in turns:
        print("You:  ", t); print("Agent:", a.chat(t).strip().replace("\n", "\n       "))
run("kiran@kestrel.test", ["I want to take leave", "casual", "next tuesday", "ok use the date you suggested", "my cousin's wedding", "yes"])
print("\nDB now:", [(l.user.name, l.start_date, l.status) for l in db.scalars(select(LeaveRequest).where(LeaveRequest.reason.like('%wedding%')))])
run("anita@kestrel.test", ["who is on leave in my team in the next two weeks and how many leaves has Ravi used?", "any requests waiting for me?"])
