from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    
    try:
        with page.expect_response(lambda r: "api/b/movie" in r.url, timeout=20000) as resp_info:
            page.goto("https://vidlink.pro/movie/tt1833673", timeout=25000)
        res = resp_info.value
        print("API Status:", res.status)
        print("API URL:", res.url)
        print("API Body:", res.text()[:500])
    except Exception as e:
        print("Error:", e)
        
    browser.close()
