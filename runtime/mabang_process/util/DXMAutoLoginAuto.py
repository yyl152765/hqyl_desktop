import json
from selenium.webdriver.common.by import By

class DXMAutoLoginAuto:
    def __init__(self, driver, base_url="https://www.dianxiaomi.com/", cookie_file="dxm_cookie.json"):
        """
        :param driver: Selenium WebDriver
        :param base_url: DXM首页URL
        :param cookie_file: 保存的cookie路径
        """
        self.driver = driver
        self.base_url = base_url
        self.cookie_file = cookie_file
        self.local_file = cookie_file.replace('cookie','localStorage')
        self.session_file = cookie_file.replace('cookie','sessionStorage')

    # ---------------- 保存登录状态 ----------------
    def save(self):
        """保存当前浏览器的 cookie / localStorage / sessionStorage"""
        self.driver.get(self.base_url)

        # 保存 cookie
        cookies = self.driver.get_cookies()
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        # 保存 localStorage
        try:
            local_storage = self.driver.execute_script("return {...localStorage};")
            with open(self.local_file, "w", encoding="utf-8") as f:
                json.dump(local_storage, f, ensure_ascii=False, indent=2)
        except:
            pass

        # 保存 sessionStorage
        try:
            session_storage = self.driver.execute_script("return {...sessionStorage};")
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(session_storage, f, ensure_ascii=False, indent=2)
        except:
            pass

    # ---------------- 仅用 cookie 登录 ----------------
    def load_cookie_only(self):
        """仅用 cookie 登录 DXM"""
        self.driver.get(self.base_url)
        self.driver.delete_all_cookies()

        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)

            for cookie in cookies:
                cookie.pop("expiry", None)
                cookie.pop("sameSite", None)
                try:
                    self.driver.add_cookie(cookie)
                except Exception as e:
                    print("跳过 cookie:", cookie, "原因:", e)
        except FileNotFoundError:
            print(f"⚠ cookie 文件不存在: {self.cookie_file}")

        self.driver.refresh()

    # ---------------- 获取 DXM_TOKEN ----------------
    def get_token(self, token_name="DXM_TOKEN"):
        try:
            return self.driver.execute_script(f"return localStorage.getItem('{token_name}');")
        except:
            return None

    # ---------------- 检测登录状态 ----------------
    def is_logged_in(self):
        """只用 cookie 判断登录状态"""
        cookies = self.driver.get_cookies()
        return len(cookies) > 0  # 有 cookie 就认为登录成功

    # ---------------- 恢复登录并返回参数 ----------------
    def restore_and_get(self):
        """
        使用 cookie 恢复登录，并返回：
        {
            "logged_in": True/False,
            "DXM_TOKEN": "",
            "cookies": [],
            "localStorage": {},
            "sessionStorage": {}
        }
        """
        # 仅用 cookie 登录
        self.load_cookie_only()

        dxm_token = self.get_token()  # 可能为 None
        cookies = self.driver.get_cookies()

        try:
            local_storage = self.driver.execute_script("return {...localStorage};")
        except:
            local_storage = {}

        try:
            session_storage = self.driver.execute_script("return {...sessionStorage};")
        except:
            session_storage = {}

        # ✅ 只用 cookie 判断登录状态
        logged_in = len(cookies) > 0

        return {
            "logged_in": logged_in,
            "DXM_TOKEN": dxm_token,
            "cookies": cookies,
            "localStorage": local_storage,
            "sessionStorage": session_storage
        }
