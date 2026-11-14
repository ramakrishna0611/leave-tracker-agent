"""HTTP-level tests of the FastAPI backend (in-memory database, no LLM calls)."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from leave_tracker import api
from leave_tracker.models import Base
from leave_tracker.seed import seed


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as s:
        seed(s)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    api.app.dependency_overrides[api.get_db] = override
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def login(c: TestClient, code: str, org_pw: str, email: str):
    assert c.post("/api/login/org", json={"code": code, "password": org_pw}).status_code == 200
    r = c.post("/api/login/user", json={"email": email, "password": "Welcome@123"})
    assert r.status_code == 200, r.text


def free_day(c: TestClient) -> str:
    """A future working day the logged-in user can book (uses the API's own suggestion)."""
    for offset in range(21, 60):
        d = (date.today() + timedelta(days=offset)).isoformat()
        if c.post("/api/check", json={"leave_type": "casual", "start": d, "end": d}).json()["ok"]:
            return d
    raise AssertionError("no free day found")


def test_requires_login(client):
    assert client.get("/api/me").status_code == 401


def test_user_step_needs_company_step(client):
    r = client.post("/api/login/user", json={"email": "kiran@kestrel.test", "password": "Welcome@123"})
    assert r.status_code == 401


def test_wrong_company_password(client):
    assert client.post("/api/login/org", json={"code": "KSTL01", "password": "nope"}).status_code == 401


def test_me_and_calendar(client):
    login(client, "KSTL01", "kestrel@2026", "kiran@kestrel.test")
    me = client.get("/api/me").json()
    assert me["role"] == "DEV" and me["project"] == "Phoenix CRM" and me["balance"]["casual"]["allowed"] == 12
    cal = client.get(f"/api/calendar?month={date.today():%Y-%m}").json()
    assert len(cal["days"]) >= 28


def test_apply_approve_flow(client):
    login(client, "KSTL01", "kestrel@2026", "kiran@kestrel.test")
    day = free_day(client)
    r = client.post("/api/leaves", json={"leave_type": "casual", "start": day, "end": day, "reason": "Exam"})
    assert r.status_code == 200, r.text
    leave_id = r.json()["leave"]["id"]
    client.post("/api/logout")

    login(client, "KSTL01", "kestrel@2026", "anita@kestrel.test")
    assert leave_id in [a["id"] for a in client.get("/api/approvals").json()]
    assert client.post(f"/api/approvals/{leave_id}", json={"approve": True}).status_code == 200
    board = client.get("/api/dashboard").json()
    assert board["manager"] and any(lane["name"] == "Kiran Reddy" for lane in board["lanes"])


def test_developer_cannot_approve_and_has_no_usage(client):
    login(client, "KSTL01", "kestrel@2026", "kiran@kestrel.test")
    day = free_day(client)
    leave_id = client.post("/api/leaves", json={"leave_type": "casual", "start": day, "end": day,
                                                "reason": "Exam"}).json()["leave"]["id"]
    client.post("/api/logout")
    login(client, "KSTL01", "kestrel@2026", "ravi@kestrel.test")
    assert client.post(f"/api/approvals/{leave_id}", json={"approve": True}).status_code == 403
    assert "usage" not in client.get("/api/dashboard").json()


def test_other_company_cannot_touch_leave(client):
    login(client, "KSTL01", "kestrel@2026", "kiran@kestrel.test")
    day = free_day(client)
    leave_id = client.post("/api/leaves", json={"leave_type": "casual", "start": day, "end": day,
                                                "reason": "Exam"}).json()["leave"]["id"]
    client.post("/api/logout")
    login(client, "SAFR02", "saffron@2026", "sara@saffron.test")          # PM at the other company
    assert client.post(f"/api/approvals/{leave_id}", json={"approve": True}).status_code == 403
    assert client.post(f"/api/leaves/{leave_id}/cancel").status_code == 403


def test_pm_leave_creates_cover_notice_for_ba(client):
    login(client, "KSTL01", "kestrel@2026", "anita@kestrel.test")
    day = free_day(client)
    assert client.post("/api/leaves", json={"leave_type": "earned", "start": day, "end": day,
                                            "reason": "Conference"}).status_code == 200
    client.post("/api/logout")
    login(client, "KSTL01", "kestrel@2026", "meera@kestrel.test")
    notices = client.get("/api/me").json()["cover_notices"]
    assert len(notices) == 1 and notices[0]["pm"] == "Anita Rao"
    assert client.post(f"/api/cover/{notices[0]['id']}/ack").status_code == 200
    assert client.get("/api/me").json()["cover_notices"] == []


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Away" in r.text
