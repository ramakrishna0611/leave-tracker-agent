"""Drive the UI with the locally installed Edge and save screenshots to docs/ (no browser download)."""
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8000"
OUT = "docs"
def login(page, code, pw, email):
    page.goto(BASE); page.wait_for_selector("#org-form")
    page.fill("#org-form [name=code]", code); page.fill("#org-form [name=password]", pw); page.click("#org-form button")
    page.wait_for_selector("#user-form:not([hidden])")
    page.fill("#user-form [name=email]", email); page.fill("#user-form [name=password]", "Welcome@123")
    page.click("#user-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])"); page.wait_for_timeout(1000)
def logout(page):
    page.click("#logout"); page.wait_for_selector("#login:not([hidden])")
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    page = b.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE); page.wait_for_timeout(900); page.screenshot(path=f"{OUT}/login.png")

    # Developer: clash on the calendar, use suggestion, request; then ask the assistant
    login(page, "ACME01", "acme@2026", "kiran@acme.test")
    page.click('.day[data-date="2026-11-24"]'); page.wait_for_selector(".check-line.bad"); page.wait_for_timeout(300)
    page.screenshot(path=f"{OUT}/dev_clash.png")
    page.click(".check-line .btn"); page.wait_for_selector(".check-line.ok")
    page.fill("#reason", "Doctor visit for my mother"); page.click("#apply-btn"); page.wait_for_timeout(1200)
    page.fill("#chat-input", "Who is away next week?"); page.click("#send")
    page.wait_for_selector(".msg.bot >> nth=1", timeout=90000); page.wait_for_timeout(800)
    page.screenshot(path=f"{OUT}/dev.png")
    logout(page)

    # PM books leave (BA must cover), sees Kiran's request on the board
    login(page, "ACME01", "acme@2026", "anita@acme.test")
    r = page.request.post(f"{BASE}/api/leaves", data={"leave_type": "earned", "start": "2026-11-30", "end": "2026-12-01", "reason": "Conference"})
    print("PM leave:", r.status, r.json().get("message"))
    page.reload(); page.wait_for_selector("#app:not([hidden])"); page.wait_for_timeout(1200)
    page.screenshot(path=f"{OUT}/pm.png", full_page=True)
    logout(page)

    # BA sees the cover pop-up
    login(page, "ACME01", "acme@2026", "meera@acme.test")
    page.wait_for_selector("#cover-modal[open]"); page.wait_for_timeout(400)
    page.screenshot(path=f"{OUT}/ba_cover.png")
    page.click("#cover-ack"); logout(page)

    # Mobile
    m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    login(m, "ACME01", "acme@2026", "anita@acme.test")
    m.click('#tabs [data-tab="board"]'); m.wait_for_timeout(500)
    m.screenshot(path=f"{OUT}/mobile.png")
    print("page errors:", errors)
    b.close()
