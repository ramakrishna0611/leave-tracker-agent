"""LLM agent that talks to employees and calls LeaveService through tools.

Safety design
- The LLM never touches the database. Each tool is a thin wrapper around LeaveService,
  which applies permissions, clash rules and balances for the logged-in user only.
- Writes are two-phase: the model can only PROPOSE a booking, cancellation or decision.
  The proposal runs only when the user's own latest message is an explicit "yes"
  (or the UI's Confirm button calls `confirm()` directly), so the model cannot act on its own.
"""
import json
import re
from datetime import date, timedelta

from openai import OpenAI

from leave_tracker.config import GROQ_API_KEY, LEAVE_TYPES, LLM_BASE_URL, LLM_MODEL
from leave_tracker.models import Holiday, Organization
from leave_tracker.policy import lookup as policy_lookup
from leave_tracker.service import LeaveService, PermissionDenied, fmt

MAX_TOOL_ROUNDS = 6
YES = re.compile(r"^\s*(yes|y|yeah|yep|confirm|confirmed|ok|okay|sure|go ahead|book it|do it|proceed)\b[\s.!]*",
                 re.IGNORECASE)


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _tool(name: str, description: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


DATE = {"type": "string", "description": "ISO date YYYY-MM-DD"}
TOOLS = [
    _tool("get_my_balance", "Leave balance of the logged-in user for each leave type (allowed, used, pending, remaining)."),
    _tool("get_member_balance", "Leave balance of a colleague in the user's project. Only PMs or acting PMs may use it.",
          {"name": {"type": "string", "description": "First name or full name"}}, ["name"]),
    _tool("who_is_on_leave", "Leaves (approved and pending) of the user's project team overlapping a date range.",
          {"start_date": DATE, "end_date": DATE}, ["start_date", "end_date"]),
    _tool("team_usage", "Leave days used, pending and remaining per team member this year. PM or acting PM only.",
          {"year": {"type": "integer"}}),
    _tool("check_leave", "Check if the user can take leave: working days, remaining balance, clashes and a suggested "
                         "free window if blocked. Does not book anything.",
          {"leave_type": {"type": "string", "enum": list(LEAVE_TYPES)}, "start_date": DATE, "end_date": DATE},
          ["leave_type", "start_date", "end_date"]),
    _tool("suggest_dates", "Nearest free window of N working days on or after a date for the user.",
          {"days": {"type": "integer"}, "after": DATE}, ["days", "after"]),
    _tool("my_leaves", "The user's own leave requests with ids and statuses."),
    _tool("pending_approvals", "Leave requests waiting for the user's decision (PM or acting PM)."),
    _tool("propose_leave", "Prepare a leave booking for the user to confirm. Call only after check_leave passed and "
                           "the user has given a reason. Nothing is saved until the user says yes.",
          {"leave_type": {"type": "string", "enum": list(LEAVE_TYPES)}, "start_date": DATE, "end_date": DATE,
           "reason": {"type": "string"}}, ["leave_type", "start_date", "end_date", "reason"]),
    _tool("propose_cancel", "Prepare cancelling one of the user's own leaves; the user must confirm.",
          {"leave_id": {"type": "integer"}}, ["leave_id"]),
    _tool("propose_decision", "Prepare approving or rejecting a team member's pending request (PM or acting PM); "
                              "the user must confirm.",
          {"leave_id": {"type": "integer"}, "approve": {"type": "boolean"}, "note": {"type": "string"}},
          ["leave_id", "approve"]),
    _tool("policy_lookup", "Search the user's company leave policy (notice periods, carry-forward, sick notes, "
                           "approval rules). Answer policy questions only from what it returns.",
          {"question": {"type": "string"}}, ["question"]),
    _tool("confirm_pending_action", "Execute the proposed action. Works only if the user's latest message clearly says yes."),
]


class LeaveAgent:
    def __init__(self, service: LeaveService, client: OpenAI | None = None, model: str = LLM_MODEL):
        self.svc = service
        self.model = model
        self.client = client or OpenAI(api_key=GROQ_API_KEY, base_url=LLM_BASE_URL, max_retries=6,
                                       default_headers={"User-Agent": "leave-tracker-agent"})
        self.pending: dict | None = None             # the one proposed write action awaiting confirmation
        self.last_user_message = ""
        self.messages: list[dict] = [{"role": "system", "content": self._system_prompt()}]

    # ---------- prompt ----------
    def _system_prompt(self) -> str:
        u, today = self.svc.user, self.svc.today
        calendar = ", ".join(f"{(today + timedelta(days=i)).strftime('%a')} {(today + timedelta(days=i)).isoformat()}"
                             for i in range(0, 21))
        holidays = self.svc.db.query(Holiday).filter(Holiday.org_id == u.org_id, Holiday.day >= today).all()
        role = {"PM": "Project Manager", "DEV": "Developer", "BA": "Business Analyst"}[u.role]
        monday = today - timedelta(days=today.weekday())
        weeks = (f"This week: {fmt(monday)} to {fmt(monday + timedelta(days=6))}. "
                 f"Next week: {fmt(monday + timedelta(days=7))} to {fmt(monday + timedelta(days=13))}. "
                 f"Week after: {fmt(monday + timedelta(days=14))} to {fmt(monday + timedelta(days=20))}.")
        if today.weekday() >= 5:
            weeks += " Today is a weekend, so 'this coming week' means next week."
        acting = " The PM is away today, so you are ACTING PM and may approve requests." if self.svc.is_acting_pm() else ""
        return f"""You are the leave assistant for {u.name}, a {role} on project "{u.project.name}".{acting}
Today is {fmt(today)} ({today.isoformat()}). Next 3 weeks: {calendar}.
{weeks}
Company holidays ahead: {', '.join(f'{h.name} {h.day.isoformat()}' for h in holidays) or 'none'}.
Leave types: {', '.join(LEAVE_TYPES)}. Weekends and holidays are not counted as leave days.

To APPLY for leave, follow these steps in order, one question at a time:
1. Ask which leave type, showing the remaining balance of each (call get_my_balance).
2. Ask for the dates or number of days. Convert phrases like "next Friday" or "3 days from the 20th" to exact ISO dates.
3. Call check_leave. If it is not ok, explain the problem plainly; if there is a clash, name the colleague and offer
   the suggested dates. A clash or low balance can never be overridden, so never offer to keep blocked dates.
   Do not continue until a valid date range is agreed.
4. Ask for the reason.
5. Call propose_leave and show a one-line summary (type, dates, working days, reason) and ask "Shall I submit it?".
6. Only when the user replies yes, call confirm_pending_action.

For policy questions (notice, carry-forward, sick notes, who approves), call policy_lookup and answer only from the
returned sections, naming the section, e.g. (Policy: Sick leave).

Rules: use tools for every fact about leave; never guess balances, dates or names. You can only act for {u.name};
you cannot book or cancel leave for anyone else, and you cannot see other projects or companies.
Approvals and cancellations also go through propose_* and then confirm_pending_action after the user says yes.
Keep replies short and friendly. Copy dates and weekdays exactly as the tools return them (e.g. Thu 26 Nov 2026); never work out a weekday yourself."""

    # ---------- tools ----------
    def _run_tool(self, name: str, args: dict) -> dict:
        s = self.svc
        try:
            if name == "get_my_balance":
                return s.balance()
            if name == "get_member_balance":
                m = s.find_member(args["name"])
                return {"name": m.name, "balance": s.balance(m)}
            if name == "who_is_on_leave":
                start, end = _date(args["start_date"]), _date(args["end_date"])
                rows = s.who_is_on_leave(start, end)
                return {"period": f"{fmt(start)} to {fmt(end)}",
                        "leaves": [{**r, "start": fmt(r["start"]), "end": fmt(r["end"])} for r in rows]}
            if name == "team_usage":
                return {"team": s.team_usage(args.get("year"))}
            if name == "check_leave":
                c = s.check_request(args["leave_type"], _date(args["start_date"]), _date(args["end_date"]))
                return {"ok": c.ok, "working_days": c.days, "remaining_after_request": (c.remaining - c.days)
                        if c.ok and c.remaining is not None else c.remaining, "problems": c.problems,
                        "suggested_start": fmt(c.suggestion[0]) if c.suggestion else None,
                        "suggested_end": fmt(c.suggestion[1]) if c.suggestion else None,
                        "requested": f"{fmt(_date(args['start_date']))} to {fmt(_date(args['end_date']))}"}
            if name == "suggest_dates":
                sug = s.suggest_dates(int(args["days"]), _date(args["after"]))
                return {"start": fmt(sug[0]), "end": fmt(sug[1])} if sug else {"result": "none found"}
            if name == "my_leaves":
                return {"leaves": [{"id": lv.id, "type": lv.leave_type, "start": fmt(lv.start_date),
                                    "end": fmt(lv.end_date), "days": lv.days, "status": lv.status}
                                   for lv in s.my_leaves()]}
            if name == "pending_approvals":
                return {"requests": [{"id": lv.id, "name": lv.user.name, "type": lv.leave_type,
                                      "start": fmt(lv.start_date), "end": fmt(lv.end_date), "days": lv.days,
                                      "reason": lv.reason} for lv in s.pending_approvals()]}
            if name == "propose_leave":
                start, end = _date(args["start_date"]), _date(args["end_date"])
                if not str(args.get("reason", "")).strip():
                    return {"ok": False, "problem": "Ask the user for a reason first."}
                c = s.check_request(args["leave_type"], start, end)
                if not c.ok:
                    return {"ok": False, "problems": c.problems}
                self.pending = {"action": "book", "leave_type": args["leave_type"], "start": start, "end": end,
                                "reason": args["reason"].strip(), "days": c.days}
                return {"ok": True, "awaiting_user_confirmation": True, "working_days": c.days}
            if name == "propose_cancel":
                lv = next((x for x in s.my_leaves() if x.id == int(args["leave_id"])), None)
                if lv is None:
                    return {"ok": False, "problem": "No such leave of yours."}
                self.pending = {"action": "cancel", "leave_id": lv.id,
                                "summary": f"{lv.leave_type} {fmt(lv.start_date)} to {fmt(lv.end_date)}"}
                return {"ok": True, "awaiting_user_confirmation": True, "leave": self.pending["summary"]}
            if name == "propose_decision":
                lv = next((x for x in s.pending_approvals() if x.id == int(args["leave_id"])), None)
                if lv is None:
                    return {"ok": False, "problem": "That request is not waiting for your decision."}
                self.pending = {"action": "decide", "leave_id": lv.id, "approve": bool(args["approve"]),
                                "note": args.get("note", ""), "summary": f"{lv.user.name}: {lv.leave_type} "
                                f"{fmt(lv.start_date)} to {fmt(lv.end_date)}"}
                return {"ok": True, "awaiting_user_confirmation": True, "request": self.pending["summary"]}
            if name == "policy_lookup":
                code = s.db.get(Organization, s.org_id).code
                hits = policy_lookup(code, args["question"])
                return {"sections": hits} if hits else {"result": "Not covered by the policy; suggest contacting HR."}
            if name == "confirm_pending_action":
                if not YES.match(self.last_user_message):
                    return {"ok": False, "problem": "The user has not said yes. Ask them to confirm first."}
                return self.confirm()
            return {"error": f"Unknown tool {name}"}
        except PermissionDenied as e:
            return {"ok": False, "permission_denied": str(e)}
        except (KeyError, ValueError) as e:
            return {"ok": False, "error": f"Bad arguments: {e}"}

    def pending_summary(self) -> str | None:
        """Human-readable description of the action waiting for confirmation (shown next to the Confirm button)."""
        p = self.pending
        if p is None:
            return None
        if p["action"] == "book":
            return (f"Book {p['leave_type']} leave {fmt(p['start'])} to {fmt(p['end'])} "
                    f"({p['days']} working day{'s' if p['days'] != 1 else ''}): {p['reason']}")
        if p["action"] == "cancel":
            return f"Cancel {p['summary']}"
        return f"{'Approve' if p['approve'] else 'Reject'} {p['summary']}"

    def discard(self) -> None:
        self.pending = None

    def confirm(self) -> dict:
        """Execute the pending action. Called by confirm_pending_action or directly by the UI's Confirm button."""
        p, self.pending = self.pending, None
        if p is None:
            return {"ok": False, "problem": "Nothing is waiting for confirmation."}
        try:
            if p["action"] == "book":
                r = self.svc.request_leave(p["leave_type"], p["start"], p["end"], p["reason"])
            elif p["action"] == "cancel":
                r = self.svc.cancel(p["leave_id"])
            else:
                r = self.svc.decide(p["leave_id"], p["approve"], p.get("note", ""))
        except PermissionDenied as e:
            return {"ok": False, "permission_denied": str(e)}
        return {"ok": r.ok, "result": r.message}

    # ---------- conversation ----------
    def chat(self, user_message: str) -> str:
        self.last_user_message = user_message
        self.messages.append({"role": "user", "content": user_message})
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=TOOLS,
                                                       temperature=0.2, timeout=60)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                self.messages.append({"role": "assistant", "content": msg.content or ""})
                return msg.content or ""
            self.messages.append({"role": "assistant", "content": msg.content or "",
                                  "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._run_tool(tc.function.name, args)
                self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                      "content": json.dumps(result, default=str)})
        return "Sorry, I could not finish that. Please try rephrasing."
