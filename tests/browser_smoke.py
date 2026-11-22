"""Browser smoke test of all five patched frontends against tests/devserver.py."""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8765"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots"
os.makedirs(OUT, exist_ok=True)
HERE = os.path.dirname(os.path.abspath(__file__))
OWL = os.path.join(HERE, "..", "lib", "default_ontology.owl")
errors = []

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and errors.append(f"{page.url}: {m.text}"))
    page.on("dialog", lambda d: d.accept("test") if d.type == "prompt" else d.accept())

    page.goto(BASE + "/")
    assert page.locator("a[href='/chat/']").count() >= 1, "hub link to chat missing"
    page.screenshot(path=f"{OUT}/0-hub.png", full_page=True)

    # Document importer
    page.goto(BASE + "/docs/")
    page.fill("#passwordInput", "salah"); page.keyboard.press("Enter"); page.wait_for_timeout(500)
    assert page.locator("#mainApp").is_hidden(), "wrong password accepted"
    page.fill("#passwordInput", "test"); page.keyboard.press("Enter")
    page.wait_for_selector("#mainApp:not(.hidden)")
    page.set_input_files("#fileInput", os.path.join(OUT, "sample.pdf"))
    page.click("#uploadBtn")
    page.wait_for_selector(".doc-item", timeout=10000)
    page.screenshot(path=f"{OUT}/2-docs.png", full_page=True)

    # Chatbot
    page.goto(BASE + "/chat/")
    page.wait_for_function("document.getElementById('healthStatus').textContent.includes('Connected')")
    page.fill("#questionInput", "Bagaimana budidaya cabai keriting?")
    page.click("#searchBtn")
    page.wait_for_selector("#answerSection", state="visible", timeout=20000)
    ans = page.inner_text("#answerContent")
    assert "Jawaban uji" in ans, ans
    page.screenshot(path=f"{OUT}/1-chat.png", full_page=True)

    # Drive sync (login only; Drive itself is not configured locally)
    page.goto(BASE + "/drive/")
    page.evaluate("sessionStorage.clear()"); page.reload()
    page.wait_for_selector("text=Masuk sebagai Admin", state="visible")
    page.click("text=Masuk sebagai Admin")
    page.wait_for_selector("#main-page.visible", timeout=5000)
    page.screenshot(path=f"{OUT}/3-drive.png", full_page=True)

    # Ontology updater (fresh context = no stored password → prompt on 401)
    page.goto(BASE + "/updater/")
    page.evaluate("sessionStorage.clear()")
    page.set_input_files("#csv-input", os.path.join(OUT, "sample.csv"))
    page.click("#run-btn")
    page.wait_for_selector("#results-card.show", timeout=20000)
    page.wait_for_function("!document.getElementById('dl-owl').style.pointerEvents")
    page.screenshot(path=f"{OUT}/4-updater.png", full_page=True)

    # Ontology refiner
    page.goto(BASE + "/refiner/")
    page.set_input_files("#file-input", OWL)
    page.wait_for_function("document.getElementById('badge-classes').textContent.trim() !== '0' "
                           "&& document.getElementById('badge-classes').textContent.trim() !== ''", timeout=10000)
    page.click("button.tab-btn:has-text('Consistency')")
    page.click("text=Run Consistency Check")
    page.wait_for_selector("#consistency-result .alert", timeout=10000)
    page.screenshot(path=f"{OUT}/5-refiner.png", full_page=True)
    print("refiner:", page.inner_text("#consistency-result"))
    b.close()

print("console errors:", errors or "none")
