"""All leave rules live here. The UI and the AI agent both call this class, so the rules cannot differ.

Every query is filtered by the logged-in user's organisation (tenant isolation), and every action
checks the user's role before reading or changing anything.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from leave_tracker.calendar_utils import add_working_days, overlaps, working_days
from leave_tracker.config import ALLOWANCE, LEAVE_TYPES, SUGGESTION_WINDOW_DAYS
from leave_tracker.models import (ACTIVE_STATUSES, AuditLog, CoverAssignment, Holiday, LeaveRequest,
                                  Notification, User)


class PermissionDenied(Exception):
    pass


@dataclass
class Clash:
    name: str
    role: str
    start: date
    end: date
    status: str


@dataclass
class CheckResult:
    ok: bool
    days: int = 0
    remaining: int | None = None
    problems: list[str] = field(default_factory=list)
    clashes: list[Clash] = field(default_factory=list)
    suggestion: tuple[date, date] | None = None


@dataclass
class RequestResult:
    ok: bool
    leave: LeaveRequest | None = None
    check: CheckResult | None = None
    message: str = ""


def fmt(d: date) -> str:
    """Always include the weekday so the LLM never has to work it out (it gets weekdays wrong)."""
    return d.strftime("%a %d %b %Y")


def span(start: date, end: date) -> str:
    return fmt(start) if start == end else f"{fmt(start)} to {fmt(end)}"


class LeaveService:
    def __init__(self, db: Session, user: User, today: date | None = None):
        self.db = db
        self.user = user
        self.org_id = user.org_id
        self.today = today or date.today()

    # ---------- helpers ----------
    def _holidays(self) -> set[date]:
        return set(self.db.scalars(select(Holiday.day).where(Holiday.org_id == self.org_id)))

    def _project_members(self, project_id: int | None) -> list[User]:
        if project_id is None:
            return []
        return list(self.db.scalars(select(User).where(User.org_id == self.org_id,
                                                       User.project_id == project_id)))

    def _clash_partners(self, user: User) -> list[User]:
        """Who must not be away at the same time as `user`.
        Two developers on a project cover each other; the BA covers the PM, so PM and BA cannot overlap."""
        others = [m for m in self._project_members(user.project_id) if m.id != user.id]
        if user.role == "DEV":
            return [m for m in others if m.role == "DEV"]
        if user.role == "PM":
            return [m for m in others if m.role == "BA"]
        if user.role == "BA":
            return [m for m in others if m.role == "PM"]
        return []

    def _active_leaves(self, user_ids: list[int], start: date, end: date) -> list[LeaveRequest]:
        if not user_ids:
            return []
        return list(self.db.scalars(select(LeaveRequest).where(
            LeaveRequest.org_id == self.org_id,
            LeaveRequest.user_id.in_(user_ids),
            LeaveRequest.status.in_(ACTIVE_STATUSES),
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )))

    def _get_user(self, user_id: int) -> User:
        u = self.db.get(User, user_id)
        if u is None or u.org_id != self.org_id:          # never reveal other organisations
            raise PermissionDenied("Employee not found.")
        return u

    def _get_leave(self, leave_id: int) -> LeaveRequest:
        lv = self.db.get(LeaveRequest, leave_id)
        if lv is None or lv.org_id != self.org_id:
            raise PermissionDenied("Leave request not found.")
        return lv

    def _notify(self, user_id: int, kind: str, message: str) -> None:
        self.db.add(Notification(org_id=self.org_id, user_id=user_id, kind=kind, message=message))

    def _audit(self, action: str, **details) -> None:
        clean = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in details.items()}
        self.db.add(AuditLog(org_id=self.org_id, actor_id=self.user.id, action=action, details=clean))

    def is_acting_pm(self, project_id: int | None = None, on: date | None = None) -> bool:
        """True if the current user is the BA covering for the PM of the project on that day."""
        if self.user.role != "BA":
            return False
        on = on or self.today
        project_id = project_id or self.user.project_id
        return self.db.scalar(select(CoverAssignment.id).where(
            CoverAssignment.org_id == self.org_id, CoverAssignment.ba_id == self.user.id,
            CoverAssignment.project_id == project_id, CoverAssignment.active.is_(True),
            CoverAssignment.start_date <= on, CoverAssignment.end_date >= on)) is not None

    def can_manage(self, project_id: int | None) -> bool:
        """PM of the project, or the BA acting as PM today."""
        if project_id is None or project_id != self.user.project_id:
            return False
        return self.user.role == "PM" or self.is_acting_pm(project_id)

    def _approver_for(self, requester: User) -> User | None:
        """The PM approves; if the PM is away today, the covering BA does."""
        members = self._project_members(requester.project_id)
        pm = next((m for m in members if m.role == "PM"), None)
        if pm is None:
            return None
        cover = self.db.scalar(select(CoverAssignment).where(
            CoverAssignment.org_id == self.org_id, CoverAssignment.pm_id == pm.id,
            CoverAssignment.active.is_(True), CoverAssignment.start_date <= self.today,
            CoverAssignment.end_date >= self.today))
        if cover and cover.ba_id != requester.id:
            return self._get_user(cover.ba_id)
        return pm

    # ---------- reading ----------
    def find_member(self, name: str) -> User:
        """Resolve a first name or full name to a colleague in the same project (agent helper)."""
        needle = name.strip().lower()
        matches = [m for m in self._project_members(self.user.project_id)
                   if needle in m.name.lower() or needle == m.email.split("@")[0]]
        if len(matches) != 1:
            raise PermissionDenied(f"No unique team member matches '{name}'." if not matches
                                   else f"'{name}' matches several people: " + ", ".join(m.name for m in matches))
        return matches[0]

    def balance(self, target: User | None = None, year: int | None = None) -> dict:
        target = target or self.user
        if target.id != self.user.id and not self.can_manage(target.project_id):
            raise PermissionDenied("Only the project manager (or acting PM) can see a colleague's balance.")
        year = year or self.today.year
        leaves = self.db.scalars(select(LeaveRequest).where(
            LeaveRequest.org_id == self.org_id, LeaveRequest.user_id == target.id,
            LeaveRequest.status.in_(ACTIVE_STATUSES),
            LeaveRequest.start_date >= date(year, 1, 1), LeaveRequest.start_date <= date(year, 12, 31)))
        out = {t: {"allowed": a, "used": 0, "pending": 0} for t, a in ALLOWANCE.items()}
        for lv in leaves:
            out[lv.leave_type]["used" if lv.status == "APPROVED" else "pending"] += lv.days
        for v in out.values():
            v["remaining"] = v["allowed"] - v["used"] - v["pending"]
        return out

    def who_is_on_leave(self, start: date, end: date) -> list[dict]:
        """Leaves in the viewer's project that overlap the period (every project member may see the team calendar)."""
        members = self._project_members(self.user.project_id)
        by_id = {m.id: m for m in members}
        rows = self._active_leaves(list(by_id), start, end)
        return [{"id": lv.id, "name": by_id[lv.user_id].name, "role": by_id[lv.user_id].role,
                 "type": lv.leave_type, "start": lv.start_date, "end": lv.end_date, "days": lv.days,
                 "status": lv.status} for lv in sorted(rows, key=lambda r: r.start_date)]

    def team_usage(self, year: int | None = None) -> list[dict]:
        if not self.can_manage(self.user.project_id):
            raise PermissionDenied("Only the project manager (or acting PM) can see team usage.")
        rows = []
        for m in self._project_members(self.user.project_id):
            b = self.balance(m, year)
            rows.append({"name": m.name, "role": m.role,
                         **{f"{t}_used": b[t]["used"] for t in LEAVE_TYPES},
                         "total_used": sum(b[t]["used"] for t in LEAVE_TYPES),
                         "total_pending": sum(b[t]["pending"] for t in LEAVE_TYPES),
                         "total_remaining": sum(b[t]["remaining"] for t in LEAVE_TYPES)})
        return rows

    def my_leaves(self) -> list[LeaveRequest]:
        return list(self.db.scalars(select(LeaveRequest).where(
            LeaveRequest.org_id == self.org_id, LeaveRequest.user_id == self.user.id)
            .order_by(LeaveRequest.start_date.desc())))

    def pending_approvals(self) -> list[LeaveRequest]:
        if not self.can_manage(self.user.project_id):
            return []
        ids = [m.id for m in self._project_members(self.user.project_id) if m.id != self.user.id]
        return list(self.db.scalars(select(LeaveRequest).where(
            LeaveRequest.org_id == self.org_id, LeaveRequest.user_id.in_(ids),
            LeaveRequest.status == "PENDING").order_by(LeaveRequest.start_date)))

    # ---------- checking ----------
    def check_request(self, leave_type: str, start: date, end: date, exclude_id: int | None = None,
                      for_user: User | None = None) -> CheckResult:
        user = for_user or self.user
        res = CheckResult(ok=False)
        if leave_type not in LEAVE_TYPES:
            res.problems.append(f"Unknown leave type '{leave_type}'. Choose one of: {', '.join(LEAVE_TYPES)}.")
            return res
        if end < start:
            res.problems.append("The end date is before the start date.")
            return res
        if start < self.today:
            res.problems.append("Leave cannot start in the past.")
        if start.year != end.year:
            res.problems.append("Split leave that crosses the new year into two requests (balances are yearly).")
        holidays = self._holidays()
        res.days = len(working_days(start, end, holidays))
        if res.days == 0:
            res.problems.append("Those dates are all weekends or company holidays, so no leave is needed.")

        own = [lv for lv in self._active_leaves([user.id], start, end) if lv.id != exclude_id]
        if own:
            res.problems.append(f"You already have leave from {fmt(own[0].start_date)} to {fmt(own[0].end_date)}.")

        bal = self.balance(user, start.year) if user.id == self.user.id or self.can_manage(user.project_id) else None
        if bal is not None:
            res.remaining = bal[leave_type]["remaining"]
            if res.days > res.remaining:
                res.problems.append(f"Not enough {leave_type} leave: {res.days} working days requested, "
                                    f"{res.remaining} remaining.")

        partners = {p.id: p for p in self._clash_partners(user)}
        for lv in self._active_leaves(list(partners), start, end):
            if lv.id == exclude_id:
                continue
            p = partners[lv.user_id]
            res.clashes.append(Clash(p.name, p.role, lv.start_date, lv.end_date, lv.status))
        if res.clashes:
            c = res.clashes[0]
            why = "the other developer on your project" if user.role == "DEV" else \
                  "your BA must cover for you" if user.role == "PM" else "you cover for the PM"
            res.problems.append(f"Not available: {c.name} ({c.role}) is on leave {fmt(c.start)} to {fmt(c.end)} "
                                f"and {why}.")
            if res.days:
                res.suggestion = self.suggest_dates(res.days, max(start, self.today), user=user)

        res.ok = not res.problems
        return res

    def suggest_dates(self, days_needed: int, after: date, user: User | None = None) -> tuple[date, date] | None:
        """Nearest window of `days_needed` working days on or after `after` with no clash and no own leave."""
        user = user or self.user
        holidays = self._holidays()
        partner_ids = [p.id for p in self._clash_partners(user)]
        cursor = after
        limit = after + timedelta(days=SUGGESTION_WINDOW_DAYS)
        while cursor <= limit:
            end = add_working_days(cursor, days_needed, holidays)
            start = cursor
            while start.weekday() >= 5 or start in holidays:
                start += timedelta(days=1)
            if end.year == start.year and not self._active_leaves(partner_ids + [user.id], start, end):
                return start, end
            cursor = start + timedelta(days=1)
        return None

    # ---------- changing ----------
    def request_leave(self, leave_type: str, start: date, end: date, reason: str) -> RequestResult:
        if not reason or not reason.strip():
            return RequestResult(ok=False, message="Please give a reason for the leave.")
        check = self.check_request(leave_type, start, end)
        if not check.ok:
            return RequestResult(ok=False, check=check, message=" ".join(check.problems))

        lv = LeaveRequest(org_id=self.org_id, user_id=self.user.id, leave_type=leave_type, start_date=start,
                          end_date=end, days=check.days, reason=reason.strip()[:300])
        self.db.add(lv)
        self.db.flush()                                   # gives lv an id

        if self.user.role == "PM":
            # Nobody above the PM: approve automatically (the clash rule already guaranteed the BA is free)
            ba = next((m for m in self._project_members(self.user.project_id) if m.role == "BA"), None)
            lv.status, lv.decided_at, lv.decision_note = "APPROVED", datetime.now(), "Auto-approved: BA covers"
            if ba:
                self.db.add(CoverAssignment(org_id=self.org_id, project_id=self.user.project_id, pm_id=self.user.id,
                                            ba_id=ba.id, leave_request_id=lv.id, start_date=start, end_date=end))
                self._notify(ba.id, "cover", f"{self.user.name} (PM) is on leave {fmt(start)} to {fmt(end)}. "
                                             f"You are the acting PM for these days.")
            message = f"Leave approved: {check.days} working day(s), {span(start, end)}." + \
                      (f" {ba.name} (BA) has been asked to cover." if ba else "")
        else:
            approver = self._approver_for(self.user)
            if approver:
                self._notify(approver.id, "approval_needed",
                             f"{self.user.name} requested {check.days} day(s) of {leave_type} leave, "
                             f"{fmt(start)} to {fmt(end)}: {lv.reason}")
            message = (f"Leave requested: {check.days} working day(s), {span(start, end)}. "
                       f"Waiting for approval from {approver.name if approver else 'your manager'}.")
        self._audit("leave_requested", leave_id=lv.id, type=leave_type, start=start, end=end,
                    days=check.days, status=lv.status)
        self.db.commit()
        return RequestResult(ok=True, leave=lv, check=check, message=message)

    def decide(self, leave_id: int, approve: bool, note: str = "") -> RequestResult:
        lv = self._get_leave(leave_id)
        requester = self._get_user(lv.user_id)
        if requester.id == self.user.id:
            raise PermissionDenied("You cannot approve your own leave.")
        if not self.can_manage(requester.project_id):
            raise PermissionDenied("Only the project manager (or acting PM) of this project can decide.")
        if lv.status != "PENDING":
            return RequestResult(ok=False, leave=lv, message=f"This request is already {lv.status.lower()}.")
        if approve:
            # Re-check in case something changed since the request was made
            check = self.check_request(lv.leave_type, lv.start_date, lv.end_date, exclude_id=lv.id, for_user=requester)
            blocking = [p for p in check.problems if not p.startswith("Leave cannot start in the past")]
            if blocking:
                return RequestResult(ok=False, leave=lv, check=check, message="Cannot approve: " + " ".join(blocking))
        lv.status = "APPROVED" if approve else "REJECTED"
        lv.approver_id, lv.decided_at, lv.decision_note = self.user.id, datetime.now(), note.strip()[:300] or None
        verb = "approved" if approve else "rejected"
        self._notify(requester.id, "decision", f"Your {lv.leave_type} leave {fmt(lv.start_date)} to "
                                               f"{fmt(lv.end_date)} was {verb} by {self.user.name}."
                                               + (f" Note: {note}" if note else ""))
        self._audit(f"leave_{verb}", leave_id=lv.id, requester=requester.name)
        self.db.commit()
        return RequestResult(ok=True, leave=lv, message=f"{requester.name}'s leave {verb}.")

    def cancel(self, leave_id: int) -> RequestResult:
        lv = self._get_leave(leave_id)
        if lv.user_id != self.user.id:
            raise PermissionDenied("You can only cancel your own leave.")
        if lv.status not in ACTIVE_STATUSES:
            return RequestResult(ok=False, leave=lv, message=f"This request is already {lv.status.lower()}.")
        if lv.start_date < self.today:
            return RequestResult(ok=False, leave=lv, message="Leave that has already started cannot be cancelled here.")
        lv.status = "CANCELLED"
        for cover in self.db.scalars(select(CoverAssignment).where(CoverAssignment.leave_request_id == lv.id)):
            cover.active = False
            self._notify(cover.ba_id, "cancelled", f"{self.user.name} cancelled leave {fmt(lv.start_date)} to "
                                                   f"{fmt(lv.end_date)}. You no longer need to cover.")
        self._audit("leave_cancelled", leave_id=lv.id)
        self.db.commit()
        return RequestResult(ok=True, leave=lv, message=f"Cancelled leave {span(lv.start_date, lv.end_date)}.")

    # ---------- notifications ----------
    def notifications(self, unread_only: bool = True) -> list[Notification]:
        q = select(Notification).where(Notification.org_id == self.org_id, Notification.user_id == self.user.id)
        if unread_only:
            q = q.where(Notification.read.is_(False))
        return list(self.db.scalars(q.order_by(Notification.created_at.desc())))

    def mark_read(self, notification_id: int) -> None:
        n = self.db.get(Notification, notification_id)
        if n and n.user_id == self.user.id:
            n.read = True
            self.db.commit()

    def pending_cover_notices(self) -> list[CoverAssignment]:
        """For the BA's login pop-up: upcoming or current cover duties not yet acknowledged."""
        return list(self.db.scalars(select(CoverAssignment).where(
            CoverAssignment.org_id == self.org_id, CoverAssignment.ba_id == self.user.id,
            CoverAssignment.active.is_(True), CoverAssignment.acknowledged.is_(False),
            CoverAssignment.end_date >= self.today)))

    def acknowledge_cover(self, cover_id: int) -> None:
        c = self.db.get(CoverAssignment, cover_id)
        if c is None or c.ba_id != self.user.id:
            raise PermissionDenied("Cover notice not found.")
        c.acknowledged = True
        self._audit("cover_acknowledged", cover_id=c.id)
        self.db.commit()
