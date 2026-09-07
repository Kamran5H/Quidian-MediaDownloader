import sys
import time
from playwright.sync_api import sync_playwright

artifact_dir = r"C:\Users\chkam\.gemini\antigravity-ide\brain\1687af59-d367-45e9-b9da-19a3634f446b"

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox', '--disable-gpu'], headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto("http://127.0.0.1:5050")

    # Click the Search tab button to reveal search-query input
    page.click(".inner-tab-btn:has-text('Search Media by Name')")
    page.wait_for_selector("#search-query", state="visible", timeout=10000)

    print("Searching Dhoom 3...")
    page.fill("#search-query", "Dhoom 3")
    page.click("#btn-search")
    page.wait_for_selector("#search-results .media-result-card", timeout=20000)
    time.sleep(1.0)

    play_btn = page.query_selector("button.play-action")
    if play_btn:
        print("Clicking Play (No Login)...")
        play_btn.click()
        time.sleep(3.5)
        modal_path = f"{artifact_dir}\\direct_cinema_player_modal.png"
        page.screenshot(path=modal_path)
        print(f"Saved Cinema modal screenshot to: {modal_path}")
    else:
        print("Play button not found!")

    browser.close()
    print("Done!")
