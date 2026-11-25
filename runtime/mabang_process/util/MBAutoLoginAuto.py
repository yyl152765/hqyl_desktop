import json
import time
from selenium.webdriver.common.by import By


def login_with_cookie(driver, cookie_file, base_url="https://900853.private.mabangerp.com/index.htm"):
    """
    用 cookie 恢复马帮登录状态

    :param driver: Selenium WebDriver
    :param cookie_file: cookie 保存路径
    :param base_url: 马帮首页地址
    :return: True/False 是否登录成功
    """

    # ---------------- 清空再加载 ----------------
    driver.delete_all_cookies()
    driver.get("about:blank")

    # ---------------- 加载 cookie ----------------
    try:
        cookies = json.load(open(cookie_file, "r", encoding="utf-8"))

        for cookie in cookies:
            cookie.pop("expiry", None)
            cookie.pop("sameSite", None)

            try:
                driver.add_cookie(cookie)
            except Exception as e:
                print("跳过无效 cookie:", cookie)

    except FileNotFoundError:
        print("cookie 文件不存在:", cookie_file)
        return False

    # ---------------- 打开马帮 ----------------
    driver.get(base_url)
    time.sleep(2)

    # ---------------- 判断是否登录成功 ----------------
    try:
        # 马帮右上角用户名
        driver.find_element(By.CSS_SELECTOR, "span.userName, .header-user-name")
        print("cookie 登录成功！")
        return True
    except:
        print("cookie 登录失败，需要重新扫码登录")
        return False
