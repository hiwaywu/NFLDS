from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True)
    page = br.new_context().new_page()
    page.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1500)
    print("browserName():", page.evaluate("() => browserName()"))
    page.evaluate("() => diag()")
    page.wait_for_timeout(1200)
    rows = page.evaluate("() => (document.getElementById('diagBody').innerText || '').split('\\n').slice(0,6)")
    for r in rows:
        print("  ", r)
    br.close()
