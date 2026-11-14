"""The agent's safety rules are enforced in code, so they can be tested without calling the LLM."""
from datetime import date

from sqlalchemy import func, select

from leave_tracker.agent import LeaveAgent
from leave_tracker.models import LeaveRequest

from tests.conftest import TODAY


class NoLLM:                      # the tests below never call the model
    pass


def agent_for(svc, email):
    return LeaveAgent(svc(email), client=NoLLM())


def count(db):
    return db.scalar(select(func.count(LeaveRequest.id)))


def test_booking_needs_user_yes(svc, db):
    a = agent_for(svc, "kiran@kestrel.test")
    before = count(db)
    assert a._run_tool("propose_leave", {"leave_type": "casual", "start_date": "2026-12-02",
                                         "end_date": "2026-12-02", "reason": "Exam"})["ok"]
    a.last_user_message = "hmm, let me think"
    assert not a._run_tool("confirm_pending_action", {})["ok"]          # model tried to confirm on its own
    assert count(db) == before
    a.last_user_message = "yes please"
    assert a._run_tool("confirm_pending_action", {})["ok"]
    assert count(db) == before + 1


def test_propose_rejects_missing_reason_and_clash(svc):
    a = agent_for(svc, "kiran@kestrel.test")
    assert not a._run_tool("propose_leave", {"leave_type": "casual", "start_date": "2026-12-02",
                                             "end_date": "2026-12-02", "reason": " "})["ok"]
    r = a._run_tool("propose_leave", {"leave_type": "casual", "start_date": "2026-11-24",
                                      "end_date": "2026-11-24", "reason": "Trip"})    # Ravi is away
    assert not r["ok"] and "Ravi Kumar" in r["problems"][0]
    assert a.pending is None


def test_developer_cannot_propose_decisions_or_see_team_balance(svc):
    a = agent_for(svc, "ravi@kestrel.test")
    assert "permission_denied" in a._run_tool("get_member_balance", {"name": "Kiran"})
    assert "permission_denied" in a._run_tool("team_usage", {})
    assert not a._run_tool("propose_decision", {"leave_id": 1, "approve": True})["ok"]


def test_other_company_names_are_invisible(svc):
    a = agent_for(svc, "anita@kestrel.test")
    assert "permission_denied" in a._run_tool("get_member_balance", {"name": "Aditya"})


def test_cancel_only_own_leave(svc, db):
    ravi_leave = svc("ravi@kestrel.test").my_leaves()[0]
    a = agent_for(svc, "kiran@kestrel.test")
    assert not a._run_tool("propose_cancel", {"leave_id": ravi_leave.id})["ok"]


def test_system_prompt_has_calendar_and_role(svc):
    a = agent_for(svc, "meera@kestrel.test")
    prompt = a.messages[0]["content"]
    assert "Business Analyst" in prompt and TODAY.isoformat() in prompt and "Christmas" in prompt


def test_empty_model_reply_falls_back_to_tool_result(svc):
    a = agent_for(svc, "kiran@kestrel.test")
    a.messages += [{"role": "user", "content": "yes"},
                   {"role": "tool", "tool_call_id": "x", "content": '{"ok": true, "result": "Leave requested."}'}]
    assert a._last_tool_message() == "Leave requested."


def test_malformed_tool_call_is_retried_on_main_model(svc):
    """Groq rejects a malformed tool name with a 400; the agent retries on the main model instead of failing."""
    import httpx
    from openai import BadRequestError

    class Completions:
        def __init__(self):
            self.models = []

        def create(self, model, **kw):
            self.models.append(model)
            if len(self.models) == 1:
                raise BadRequestError("tool call validation failed: 'check_leave<|channel|>'",
                                      response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                                      body=None)
            return "ok"

    class Client:
        def __init__(self):
            self.chat = type("C", (), {"completions": Completions()})()

    client = Client()
    a = LeaveAgent(svc("kiran@kestrel.test"), client=client)
    assert a._complete() == "ok"
    assert len(client.chat.completions.models) == 2
