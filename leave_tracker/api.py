"""FastAPI backend: JSON API for the web UI, sessions in a signed cookie.

Run:  uvicorn leave_tracker.api:app --reload      then open http://127.0.0.1:8000
"""
import calendar as cal
import os
import secrets
import time
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from openai import RateLimitError

from leave_tracker.agent import LeaveAgent
from leave_tracker.auth import AuthError, login_organization, login_user
from leave_tracker.config import DB_URL, LEAVE_TYPES
from leave_tracker.models import Base, CoverAssignment, Holiday, Organization, User
from leave_tracker.service import LeaveService, PermissionDenied, fmt

WEB = Path(__file__).parent / "web"
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

# Hosted demos start with an empty disk: create the demo companies on first start
if os.getenv("AUTO_SEED", "1") == "1":
    with SessionLocal() as _db:
        if _db.query(Organization).count() == 0:
            from leave_tracker.seed import seed
            seed(_db)

# Public demo protection: each browser session may send this many chat messages per hour
CHAT_LIMIT_PER_HOUR = int(os.getenv("CHAT_LIMIT_PER_HOUR", "40"))
CHAT_LOG: dict[str, list[float]] = {}

app = FastAPI(title="Leave Tracker Agent")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY") or secrets.token_hex(32),
                   same_site="lax", https_only=os.getenv("HTTPS_ONLY", "0") == "1", max_age=8 * 3600)

AGENTS: dict[str, LeaveAgent] = {}        # one conversation per browser session (in memory)


# ---------- dependencies ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(401, "Please log in.")
    return user


def service(user: User = Depends(current_user), db: Session = Depends(get_db)) -> LeaveService:
    return LeaveService(db, user)


def denied(e: PermissionDenied):
    return HTTPException(403, str(e))


# ---------- schemas ----------
class OrgLogin(BaseModel):
    code: str
    password: str


class UserLogin(BaseModel):
    email: str
    password: str


class LeaveIn(BaseModel):
    leave_type: str
    start: date
    end: date
    reason: str = ""


class Decision(BaseModel):
    approve: bool
    note: str = ""


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


# ---------- auth ----------
@app.post("/api/login/org")
def login_org(body: OrgLogin, request: Request, db: Session = Depends(get_db)):
    try:
        org = login_organization(db, body.code, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e))
    request.session.clear()
    request.session["org_id"] = org.id
    return {"org": org.name}


@app.post("/api/login/user")
def login_emp(body: UserLogin, request: Request, db: Session = Depends(get_db)):
    org = db.get(Organization, request.session.get("org_id") or -1)
    if org is None:
        raise HTTPException(401, "Enter your company code first.")
    try:
        user = login_user(db, org, body.email, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e))
    request.session["user_id"] = user.id
    request.session["sid"] = secrets.token_hex(16)
    return {"name": user.name}


@app.post("/api/logout")
def logout(request: Request):
    AGENTS.pop(request.session.get("sid", ""), None)
    request.session.clear()
    return {"ok": True}


# ---------- profile ----------
def _cover_payload(svc: LeaveService) -> list[dict]:
    out = []
    for c in svc.pending_cover_notices():
        pm = svc.db.get(User, c.pm_id)
        out.append({"id": c.id, "pm": pm.name, "start": fmt(c.start_date), "end": fmt(c.end_date),
                    "now": c.start_date <= svc.today <= c.end_date})
    return out


@app.get("/api/me")
def me(svc: LeaveService = Depends(service)):
    u = svc.user
    org = svc.db.get(Organization, u.org_id)
    return {"name": u.name, "email": u.email, "role": u.role, "project": u.project.name, "org": org.name,
            "org_code": org.code, "today": svc.today.isoformat(), "acting_pm": svc.is_acting_pm(),
            "can_manage": svc.can_manage(u.project_id), "balance": svc.balance(), "leave_types": list(LEAVE_TYPES),
            "cover_notices": _cover_payload(svc), "unread": len(svc.notifications())}


# ---------- calendar ----------
@app.get("/api/calendar")
def month_view(month: str, svc: LeaveService = Depends(service)):
    try:
        y, m = map(int, month.split("-"))
        first = date(y, m, 1)
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    last = date(y, m, cal.monthrange(y, m)[1])
    holidays = {h.day: h.name for h in svc.db.scalars(select(Holiday).where(
        Holiday.org_id == svc.org_id, Holiday.day >= first, Holiday.day <= last))}
    leaves = svc.who_is_on_leave(first, last)
    partners = {p.name for p in svc._clash_partners(svc.user)}
    days = []
    d = first
    while d <= last:
        on = [{"name": lv["name"], "role": lv["role"], "status": lv["status"], "type": lv["type"],
               "mine": lv["name"] == svc.user.name} for lv in leaves if lv["start"] <= d <= lv["end"]]
        days.append({"date": d.isoformat(), "weekend": d.weekday() >= 5, "holiday": holidays.get(d),
                     "past": d < svc.today, "today": d == svc.today, "leaves": on,
                     "blocked_by": [x["name"] for x in on if x["name"] in partners]})
        d += timedelta(days=1)
    return {"month": month, "first_weekday": first.weekday(), "days": days}


@app.post("/api/check")
def check(body: LeaveIn, svc: LeaveService = Depends(service)):
    c = svc.check_request(body.leave_type, body.start, body.end)
    return {"ok": c.ok, "days": c.days, "remaining": c.remaining, "problems": c.problems,
            "suggestion": [c.suggestion[0].isoformat(), c.suggestion[1].isoformat()] if c.suggestion else None,
            "suggestion_label": f"{fmt(c.suggestion[0])} to {fmt(c.suggestion[1])}" if c.suggestion else None}


# ---------- leaves ----------
def _leave_row(lv) -> dict:
    return {"id": lv.id, "type": lv.leave_type, "start": lv.start_date.isoformat(), "end": lv.end_date.isoformat(),
            "label": f"{fmt(lv.start_date)}" + ("" if lv.start_date == lv.end_date else f" to {fmt(lv.end_date)}"),
            "days": lv.days, "status": lv.status, "reason": lv.reason, "name": lv.user.name,
            "note": lv.decision_note}


@app.post("/api/leaves")
def apply(body: LeaveIn, svc: LeaveService = Depends(service)):
    r = svc.request_leave(body.leave_type, body.start, body.end, body.reason)
    if not r.ok:
        raise HTTPException(400, r.message)
    return {"message": r.message, "leave": _leave_row(r.leave)}


@app.get("/api/leaves/mine")
def mine(svc: LeaveService = Depends(service)):
    return [_leave_row(lv) for lv in svc.my_leaves()]


@app.post("/api/leaves/{leave_id}/cancel")
def cancel(leave_id: int, svc: LeaveService = Depends(service)):
    try:
        r = svc.cancel(leave_id)
    except PermissionDenied as e:
        raise denied(e)
    if not r.ok:
        raise HTTPException(400, r.message)
    return {"message": r.message}


@app.get("/api/approvals")
def approvals(svc: LeaveService = Depends(service)):
    return [_leave_row(lv) for lv in svc.pending_approvals()]


@app.post("/api/approvals/{leave_id}")
def decide(leave_id: int, body: Decision, svc: LeaveService = Depends(service)):
    try:
        r = svc.decide(leave_id, body.approve, body.note)
    except PermissionDenied as e:
        raise denied(e)
    if not r.ok:
        raise HTTPException(400, r.message)
    return {"message": r.message}


# ---------- dashboard ----------
@app.get("/api/dashboard")
def dashboard(days: int = 21, svc: LeaveService = Depends(service)):
    start = svc.today
    end = start + timedelta(days=days - 1)
    members = svc._project_members(svc.user.project_id)
    leaves = svc.who_is_on_leave(start, end)
    order = {"PM": 0, "BA": 1, "DEV": 2}
    lanes = []
    for m in sorted(members, key=lambda x: (order[x.role], x.name)):
        bars = []
        for lv in leaves:
            if lv["name"] != m.name:
                continue
            s, e = max(lv["start"], start), min(lv["end"], end)
            bars.append({"offset": (s - start).days, "length": (e - s).days + 1, "status": lv["status"],
                         "type": lv["type"], "label": f"{fmt(lv['start'])} to {fmt(lv['end'])}"})
        lanes.append({"name": m.name, "role": m.role, "me": m.id == svc.user.id, "bars": bars})

    holidays = set(svc.db.scalars(select(Holiday.day).where(Holiday.org_id == svc.org_id)))
    axis = [{"date": (start + timedelta(days=i)).isoformat(),
             "dow": (start + timedelta(days=i)).strftime("%a")[0],
             "dom": (start + timedelta(days=i)).day,
             "off": (start + timedelta(days=i)).weekday() >= 5 or (start + timedelta(days=i)) in holidays}
            for i in range(days)]

    capacity = []
    for i, a in enumerate(axis):
        if a["off"]:
            continue
        d = start + timedelta(days=i)
        away = [lv["name"] for lv in leaves if lv["start"] <= d <= lv["end"]]
        if members and len(away) / len(members) >= 0.5:
            capacity.append({"date": fmt(d), "away": away, "percent": round(100 * (1 - len(away) / len(members)))})

    out = {"axis": axis, "lanes": lanes, "capacity": capacity, "manager": svc.can_manage(svc.user.project_id)}
    if out["manager"]:
        usage = svc.team_usage()
        for row in usage:
            row["no_leave_yet"] = row["total_used"] == 0 and row["total_pending"] == 0
        out["usage"] = usage
    # A BA sees requests for the days they will cover for the PM, even before the cover starts
    out["approvals"] = [_leave_row(lv) for lv in svc.pending_approvals()]
    return out


# ---------- notifications and cover ----------
@app.get("/api/notifications")
def notifications(svc: LeaveService = Depends(service)):
    return [{"id": n.id, "kind": n.kind, "message": n.message, "read": n.read,
             "at": n.created_at.strftime("%d %b, %H:%M")} for n in svc.notifications(unread_only=False)[:30]]


@app.post("/api/notifications/read-all")
def read_all(svc: LeaveService = Depends(service)):
    for n in svc.notifications():
        svc.mark_read(n.id)
    return {"ok": True}


@app.post("/api/cover/{cover_id}/ack")
def ack_cover(cover_id: int, svc: LeaveService = Depends(service)):
    try:
        svc.acknowledge_cover(cover_id)
    except PermissionDenied as e:
        raise denied(e)
    return {"ok": True}


# ---------- chat ----------
def _agent(request: Request, svc: LeaveService) -> LeaveAgent:
    sid = request.session.get("sid")
    agent = AGENTS.get(sid)
    if agent is None or agent.svc.user.id != svc.user.id:
        agent = AGENTS[sid] = LeaveAgent(svc)
    agent.svc = svc                          # fresh DB session for this request
    return agent


@app.post("/api/chat")
def chat(body: ChatIn, request: Request, svc: LeaveService = Depends(service)):
    now = time.time()
    recent = [t for t in CHAT_LOG.get(request.session.get("sid", ""), []) if now - t < 3600]
    if len(recent) >= CHAT_LIMIT_PER_HOUR:
        raise HTTPException(429, "Demo limit reached for this hour. The calendar and team board still work.")
    CHAT_LOG[request.session.get("sid", "")] = recent + [now]
    agent = _agent(request, svc)
    try:
        reply = agent.chat(body.message)
    except RateLimitError:
        agent.messages.pop() if agent.messages[-1]["role"] == "user" else None   # let the user simply resend
        raise HTTPException(429, "The assistant is busy (free-tier limit of the AI model). Please try again in "
                                 "about 30 seconds — the calendar works meanwhile.")
    except Exception as e:                   # LLM outage: tell the user instead of a 500
        raise HTTPException(503, f"The assistant is unavailable right now ({type(e).__name__}). "
                                 "You can still apply from the calendar.")
    return {"reply": reply, "pending": agent.pending_summary()}


@app.post("/api/chat/confirm")
def chat_confirm(request: Request, svc: LeaveService = Depends(service)):
    agent = _agent(request, svc)
    result = agent.confirm()                  # explicit button click by the user
    text = result.get("result") or result.get("problem") or result.get("permission_denied") or "Done."
    agent.messages.append({"role": "assistant", "content": f"[User clicked Confirm] {text}"})
    return {"ok": result.get("ok", False), "message": text}


@app.post("/api/chat/discard")
def chat_discard(request: Request, svc: LeaveService = Depends(service)):
    agent = _agent(request, svc)
    agent.discard()
    agent.messages.append({"role": "assistant", "content": "[User clicked Cancel] The proposed action was discarded."})
    return {"ok": True}


# ---------- static frontend ----------
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
