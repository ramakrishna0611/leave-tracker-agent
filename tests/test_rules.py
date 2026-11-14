"""Business rules. Seed (relative to Monday 16 Nov 2026):
Ravi (DEV, Phoenix) approved casual 23-25 Nov; Sneha (DEV, Atlas) sick 16-17 Nov;
Arjun (DEV, Atlas) pending earned 30 Nov-4 Dec; Rahul (DEV, Orion) pending 18 Nov; Aditya (Saffron) 23-24 Nov."""
from datetime import date

import pytest

from leave_tracker.auth import AuthError, login_organization, login_user
from leave_tracker.models import CoverAssignment
from leave_tracker.service import PermissionDenied

D = lambda m, d: date(2026, m, d)


# ---------- login ----------
def test_two_step_login(db):
    org = login_organization(db, "kstl01", "kestrel@2026")     # codes are case-insensitive
    user = login_user(db, org, "Ravi@kestrel.test", "Welcome@123")
    assert user.name == "Ravi Kumar"


def test_wrong_company_password_rejected(db):
    with pytest.raises(AuthError):
        login_organization(db, "KSTL01", "wrong")


def test_employee_cannot_log_into_other_company(db):
    saffron = login_organization(db, "SAFR02", "saffron@2026")
    with pytest.raises(AuthError):
        login_user(db, saffron, "ravi@kestrel.test", "Welcome@123")


# ---------- clash rule ----------
def test_developer_clash_blocked_with_suggestion(svc):
    res = svc("kiran@kestrel.test").request_leave("casual", D(11, 24), D(11, 24), "Personal work")
    assert not res.ok
    assert res.check.clashes[0].name == "Ravi Kumar"
    start, end = res.check.suggestion
    assert start == D(11, 26) and end == D(11, 26)        # first free working day after Ravi's leave


def test_developers_on_different_projects_do_not_clash(svc):
    res = svc("sneha@kestrel.test").request_leave("casual", D(11, 24), D(11, 24), "Errand")
    assert res.ok


def test_pending_leave_also_blocks(svc):
    res = svc("sneha@kestrel.test").check_request("casual", D(12, 1), D(12, 1))   # Arjun pending 30 Nov-4 Dec
    assert not res.ok and res.clashes[0].status == "PENDING"


def test_ba_cannot_overlap_pm(svc):
    assert svc("anita@kestrel.test").request_leave("casual", D(12, 7), D(12, 8), "Conference").ok
    res = svc("meera@kestrel.test").request_leave("casual", D(12, 8), D(12, 8), "Personal")
    assert not res.ok and "cover" in res.message


# ---------- balance and dates ----------
def test_balance_counts_only_working_days(svc):
    s = svc("kiran@kestrel.test")
    res = s.request_leave("casual", D(12, 4), D(12, 7), "Trip")             # Fri + weekend + Mon
    assert res.ok and res.leave.days == 2
    assert s.balance()["casual"] == {"allowed": 12, "used": 0, "pending": 2, "remaining": 10}


def test_holiday_not_counted(svc):
    res = svc("kiran@kestrel.test").request_leave("casual", D(12, 24), D(12, 25), "Christmas")
    assert res.ok and res.leave.days == 1                                    # 25 Dec is a holiday


def test_insufficient_balance(svc):
    res = svc("kiran@kestrel.test").request_leave("sick", D(12, 1), D(12, 9), "Surgery")   # 7 days > 6
    assert not res.ok and "Not enough sick leave" in res.message


def test_past_dates_and_reason_required(svc):
    s = svc("kiran@kestrel.test")
    assert not s.request_leave("casual", D(11, 10), D(11, 10), "Late entry").ok
    assert not s.request_leave("casual", D(12, 2), D(12, 2), "  ").ok


def test_cannot_double_book_self(svc):
    s = svc("kiran@kestrel.test")
    assert s.request_leave("casual", D(12, 2), D(12, 3), "A").ok
    assert not s.request_leave("earned", D(12, 3), D(12, 3), "B").ok


# ---------- approvals ----------
def test_pm_approves_developer_request(svc):
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    assert "Anita Rao" in req.message
    pm = svc("anita@kestrel.test")
    assert [lv.id for lv in pm.pending_approvals()] == [req.leave.id]
    assert pm.decide(req.leave.id, approve=True).ok
    assert svc("kiran@kestrel.test").balance()["casual"]["used"] == 1


def test_developer_cannot_approve(svc):
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    with pytest.raises(PermissionDenied):
        svc("ravi@kestrel.test").decide(req.leave.id, approve=True)


def test_pm_of_other_project_cannot_approve(svc):
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    with pytest.raises(PermissionDenied):
        svc("vikram@kestrel.test").decide(req.leave.id, approve=True)


# ---------- BA as acting PM ----------
def test_pm_leave_auto_approved_and_ba_notified(svc, db):
    res = svc("anita@kestrel.test").request_leave("earned", D(11, 16), D(11, 18), "Off-site")
    assert res.ok and res.leave.status == "APPROVED"
    meera = svc("meera@kestrel.test")
    notices = meera.pending_cover_notices()
    assert len(notices) == 1 and notices[0].start_date == D(11, 16)
    assert any(n.kind == "cover" for n in meera.notifications())


def test_acting_ba_can_approve_and_see_team_while_pm_away(svc):
    svc("anita@kestrel.test").request_leave("earned", D(11, 16), D(11, 18), "Off-site")   # PM away today
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    assert "Meera Shah" in req.message                     # routed to the acting PM
    meera = svc("meera@kestrel.test")
    assert meera.is_acting_pm()
    assert meera.decide(req.leave.id, approve=True).ok
    assert {r["name"] for r in meera.team_usage()} >= {"Kiran Reddy", "Ravi Kumar"}


def test_ba_without_cover_cannot_approve(svc):
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    with pytest.raises(PermissionDenied):
        svc("meera@kestrel.test").decide(req.leave.id, approve=True)


def test_cancel_pm_leave_releases_ba(svc, db):
    res = svc("anita@kestrel.test").request_leave("earned", D(12, 7), D(12, 8), "Conference")
    assert svc("anita@kestrel.test").cancel(res.leave.id).ok
    assert db.query(CoverAssignment).one().active is False
    assert svc("meera@kestrel.test").pending_cover_notices() == []


# ---------- visibility and tenant isolation ----------
def test_developer_cannot_see_colleague_balance(svc):
    ravi = svc("ravi@kestrel.test")
    with pytest.raises(PermissionDenied):
        ravi.balance(ravi.find_member("Kiran"))


def test_pm_sees_team_balance(svc):
    pm = svc("anita@kestrel.test")
    assert pm.balance(pm.find_member("Ravi"))["casual"]["used"] == 3


def test_who_is_on_leave_is_project_scoped(svc):
    rows = svc("anita@kestrel.test").who_is_on_leave(D(11, 16), D(11, 30))
    assert [r["name"] for r in rows] == ["Ravi Kumar"]                     # not Sneha (Atlas) or Aditya (Saffron)


def test_cannot_touch_other_company_records(svc):
    saffron_leave = svc("aditya@saffron.test").my_leaves()[0]
    with pytest.raises(PermissionDenied):
        svc("anita@kestrel.test").decide(saffron_leave.id, approve=False)
    with pytest.raises(PermissionDenied):
        svc("anita@kestrel.test").find_member("Aditya")


def test_audit_log_records_actions(svc, db):
    from leave_tracker.models import AuditLog
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    svc("anita@kestrel.test").decide(req.leave.id, approve=False, note="Release week")
    actions = [a.action for a in db.query(AuditLog).order_by(AuditLog.id)]
    assert actions == ["leave_requested", "leave_rejected"]


def test_request_on_days_pm_is_away_goes_to_covering_ba(svc):
    """PM is out 19-20 Nov (today is 16 Nov). A developer asking for 19 Nov is routed to the BA,
    who can decide straight away; the PM, still in the office today, can decide too."""
    assert svc("anita@kestrel.test").request_leave("earned", D(11, 19), D(11, 20), "Workshop").ok
    req = svc("kiran@kestrel.test").request_leave("casual", D(11, 19), D(11, 19), "Family visit")
    assert req.ok and "Meera Shah" in req.message
    meera = svc("meera@kestrel.test")
    assert req.leave.id in [lv.id for lv in meera.pending_approvals()]
    assert not meera.is_acting_pm()                        # cover has not started yet...
    assert meera.decide(req.leave.id, approve=True).ok     # ...but she covers those days, so she decides


def test_request_outside_pm_absence_still_goes_to_pm(svc):
    svc("anita@kestrel.test").request_leave("earned", D(11, 19), D(11, 20), "Workshop")
    req = svc("kiran@kestrel.test").request_leave("casual", D(12, 2), D(12, 2), "Exam")
    assert "Anita Rao" in req.message
    meera = svc("meera@kestrel.test")
    assert meera.pending_approvals() == []
    with pytest.raises(PermissionDenied):
        meera.decide(req.leave.id, approve=True)
