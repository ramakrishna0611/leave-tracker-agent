"""Terminal chat for quick testing.   Run:  python -m leave_tracker.cli"""
from getpass import getpass

from leave_tracker.agent import LeaveAgent
from leave_tracker.auth import AuthError, login_organization, login_user
from leave_tracker.models import make_session_factory
from leave_tracker.service import LeaveService


def main() -> None:
    db = make_session_factory()()
    try:
        org = login_organization(db, input("Company code: "), getpass("Company password: "))
        user = login_user(db, org, input("Email: "), getpass("Password: "))
    except AuthError as e:
        print(e)
        return
    svc = LeaveService(db, user)
    agent = LeaveAgent(svc)
    print(f"\nHi {user.name} ({user.role}, {user.project.name}). Type 'quit' to exit.")
    for c in svc.pending_cover_notices():
        print(f"!! Cover notice: the PM is on leave {c.start_date} to {c.end_date}; you are acting PM.")
    while (text := input("\nYou: ").strip()).lower() not in {"quit", "exit"}:
        print("Agent:", agent.chat(text))


if __name__ == "__main__":
    main()
