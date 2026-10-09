import os
import time

from playwright.sync_api import sync_playwright


def log_request(request):
    url = request.url
    if "api/" in url or "login" in url or "products" in url:
        print(f"[REQ] {request.method} {url}")
        if request.post_data:
            print(f"  PostData: {request.post_data}")
        print(f"  Headers: {request.headers}")


def log_response(response):
    url = response.url
    if "api/" in url or "login" in url or "products" in url:
        print(f"[RESP] {response.status} {url}")
        try:
            if "application/json" in response.headers.get("content-type", ""):
                print(f"  Body: {response.text()[:2000]}")
        except Exception as exc:
            print(f"  Could not read body: {exc}")


def credentials_from_environment() -> tuple[str, str]:
    username = os.environ.get("ECHOTIK_USERNAME", "").strip()
    password = os.environ.get("ECHOTIK_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("请先设置 ECHOTIK_USERNAME 和 ECHOTIK_PASSWORD 环境变量")
    return username, password


def run():
    username, password = credentials_from_environment()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.on("request", log_request)
        page.on("response", log_response)

        print("Navigating to login page...")
        page.goto("https://echotik.live/login")
        page.wait_for_load_state("networkidle")

        print(f"Frame count: {len(page.frames)}")
        for index, frame in enumerate(page.frames):
            print(f"Frame {index}: url={frame.url}")

        page.wait_for_selector("input", timeout=5000)
        inputs = page.locator("input")
        print(f"Found {inputs.count()} inputs on main page.")
        for index in range(inputs.count()):
            item = inputs.nth(index)
            print(
                f"Input {index}: placeholder={item.get_attribute('placeholder')}, "
                f"type={item.get_attribute('type')}"
            )

        email_selector = "input[type='email']" if page.locator("input[type='email']").count() else "input[type='text']"
        page.fill(email_selector, username)
        page.fill("input[type='password']", password)

        print("Waiting for manual interactions/inspection...")
        time.sleep(120)
        browser.close()


if __name__ == "__main__":
    run()
