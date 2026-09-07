from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    
    def log_req(r):
        if any(x in r.url for x in ["api", "m3u8", "stream", "video", "manifest", "embed", "player"]):
            print(f"[{r.method}] {r.url[:120]}")
            
    page.on("request", log_req)
    
    print("Navigating to VidLink...")
    try:
        page.goto("https://vidlink.pro/movie/tt1833673", timeout=25000, wait_until="domcontentloaded")
        time.sleep(6)
    except Exception as e:
        print("Nav error:", e)
        
    browser.close()
