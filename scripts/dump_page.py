import sys
import time
from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        
        print("Navigating to login page...")
        page.goto("https://echotik.live/login")
        page.wait_for_load_state("networkidle")
        
        print("Dumping Page Content...")
        print(page.content()[:50000]) # Print first 50kb of content to see input elements
        
        browser.close()

if __name__ == "__main__":
    run()
