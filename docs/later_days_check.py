"""Does the page show fixtures for a date beyond the stream window?

    python later_days_check.py [YYYY-MM-DD]
"""
import sys

from playwright.sync_api import sync_playwright

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-11-10"

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True)
    page = br.new_context(viewport={"width": 900, "height": 1200}).new_page()
    page.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.evaluate("(d) => { curDate = d; loadGames(); }", DAY)
    page.wait_for_timeout(4000)
    info = page.evaluate("""() => {
        const rows = [...document.querySelectorAll('#list .row')];
        const notes = [...document.querySelectorAll('#list .note')].map(n => n.textContent);
        return {
            date: document.querySelector('#dateLabel') ? document.querySelector('#dateLabel').textContent : null,
            rows: rows.length,
            first: rows[0] ? rows[0].innerText.replace(/\\n/g, ' | ') : null,
            notes: notes,
            header: document.querySelector('#list .sect') ? document.querySelector('#list .sect').innerText : null,
        };
    }""")
    page.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\later_day.png")
    print(info)
    br.close()
