import os
import time
from playwright.sync_api import sync_playwright

artifact_dir = r"C:\Users\chkam\.gemini\antigravity-ide\brain\1b6a78eb-4114-46cf-b3c0-a2234d4581bf"
os.makedirs(artifact_dir, exist_ok=True)
screenshot_path = os.path.join(artifact_dir, "folder_explorer_modal.png")

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox', '--disable-gpu'], headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto("http://127.0.0.1:5050")
    page.wait_for_selector("#dest-suggestions", state="attached", timeout=10000)

    # Trigger opening destination modal
    page.evaluate("openDestinationModal()")
    page.wait_for_selector("#destination-modal:not(.hidden)", state="visible", timeout=5000)
    
    # Wait for the folder list and drives to populate
    page.wait_for_selector(".explorer-drive-btn", timeout=5000)
    time.sleep(1.0)

    # Click on D:\ drive button
    page.locator(".explorer-drive-btn").filter(has_text="D:").click()
    time.sleep(0.8)

    # Click drill-down on Documents folder
    page.locator(".explorer-folder-item").filter(has_text="Documents").locator(".btn-folder-open").first.click()
    time.sleep(1.0)

    sub_screenshot_path = os.path.join(artifact_dir, "folder_explorer_subfolder.png")
    modal = page.query_selector(".modal-card")
    if modal:
        modal.screenshot(path=sub_screenshot_path)
        print(f"Subfolder modal screenshot saved to: {sub_screenshot_path}")

    browser.close()
    print("Verification complete.")
