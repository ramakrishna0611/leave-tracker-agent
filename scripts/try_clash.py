from datetime import date
from sqlalchemy import select
from leave_tracker.models import User, make_session_factory
from leave_tracker.seed import seed
from leave_tracker.service import LeaveService
from leave_tracker.agent import LeaveAgent
TODAY = date(2026, 11, 16)
db = make_session_factory("sqlite:///:memory:")(); seed(db, today=TODAY)
u = db.scalar(select(User).where(User.email == "kiran@kestrel.test"))
a = LeaveAgent(LeaveService(db, u, today=TODAY))
for t in ["I need 1 day casual leave on 24 Nov", "ok take the suggested day", "doctor visit for my mother", "yes"]:
    print("You:  ", t); print("Agent:", a.chat(t).strip().replace("\n", "\n       "))
