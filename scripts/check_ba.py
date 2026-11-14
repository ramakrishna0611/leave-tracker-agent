from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge"); page = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []; page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1:8000"); page.wait_for_selector("#org-form")
    page.fill("#org-form [name=code]", "KSTL01"); page.fill("#org-form [name=password]", "kestrel@2026"); page.click("#org-form button")
    page.wait_for_selector("#user-form:not([hidden])")
    page.fill("#user-form [name=email]", "meera@kestrel.test"); page.fill("#user-form [name=password]", "Welcome@123")
    page.click("#user-form button[type=submit]"); page.wait_for_selector("#app:not([hidden])"); page.wait_for_timeout(1500)
    if page.is_visible("#cover-modal[open]"): page.keyboard.press("Escape")
    print("approvals visible:", page.is_visible("#approvals-block"), "|", page.inner_text("#approvals")[:200].replace("\n", " "))
    page.screenshot(path="docs/ba_approvals.png"); print("errors:", errs); b.close()
