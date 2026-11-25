import datetime
import sys, os
import time
import re
from time import sleep
# rocess_config_path = os.path.dirname(__file__)
# config_path = os.path.dirname(os.path.dirname(__file__))
# sys.path.append(config_path)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from util.yingdao_util import read_txt,append_txt
from util.email_util import extract_masked_email,split_email,get_opt_code
import traceback

def parse_text_info(text: str,country: str) -> dict:
    """
    从字符串中提取：
    - country: 国家（菲律宾）
    - number: 数字编号（字符串）
    - is_warehouse: 是否包含“仓发”
    """
    result = {
        "country": None,
        "number": None,
        "is_warehouse": False
    }
    if not text:
        return result

    # 是否包含“仓发”
    result["is_warehouse"] = "仓发" in text

    # 提取 国家
    if country in text:
        result["country"] = True
    else:
        result["country"] = False
    # 提取 国家 + 数字
    m = re.search(rf'({country})(\d+)', text)
    if m:
        # result["country"] = m.group(1)
        result["number"] = m.group(2)

    return result
def structured_match_shop(query: str, target: str,country: str) -> bool:

    q = parse_text_info(query,country)
    t = parse_text_info(target,country)
    # 国家或编号不一致，直接失败
    if q["country"] != t["country"]:
        return False
    if q["number"] != t["number"]:
        return False
    if q["number"] is None and t["number"] is None:
        if target.endswith(query):
            return True
    if q["is_warehouse"] != t["is_warehouse"]:
        return False
    # 查询是“仓发” → 目标必须也是仓发
    if q["is_warehouse"]:
        return t["is_warehouse"]

    # 查询不是仓发 → 目标也必须不是仓发
    return not t["is_warehouse"]



def shopee_email_from_otp_modal(self) -> str:
    try:
        text_elem = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "p.text-desc.sms")
            )
        )
        text = text_elem.text  # 获取完整文本
        print("弹框文本:", text)

        email = extract_masked_email(text)
        print("提取到邮箱:", email)
        return email

    except Exception as e:
        print("获取邮箱失败:", e)
        return None



def shopee_input_email_otp_and_confirm(self,store_name, otp: str, timeout: int = 15) -> bool:
    """
    输入邮箱验证码并点击确认按钮
    otp: 验证码字符串
    timeout: 等待弹框和按钮出现的超时时间
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import traceback

    try:
        # 等待验证码弹框出现
        modal = WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.eds-modal__mask"))
        )
        print("检测到验证码弹框")

        # 找到输入框并输入验证码
        input_boxs = self.find_elements(By.CSS_SELECTOR, "input.eds-input__input")
        input_boxs[2].clear()
        input_boxs[2].send_keys(otp)
        print(f"已输入验证码: {otp}")

        # 找到确认按钮并点击
        confirm_btn = self.find_element(
            By.XPATH, ".//button[contains(@class,'eds-button') and .//span[text()='确认']]"
        )
        confirm_btn.click()
        print("点击确认按钮")

        sleep(20)
        if 'account/signin' not in self.driver.current_url:
            print("验证码弹框已关闭，操作完成")
            print(f"检测到{self.driver.current_url}不是登录地址")
            return True
        else:
            print(f"{store_name}验证码输入错误，操作完成")
            return False


    except Exception as e:
        print(f"输入验证码异常: {e}")
        print(traceback.format_exc())
        return False

def shopee_check_and_login_version1(self, timeout=30) -> bool:
    """
    检查是否已登录 → 如果未登录则点击登录按钮 → 等待登录成功
    登录成功判断标准：离开登录页，兼容各站点登录页的 SPA 根节点
    """
    self.driver.implicitly_wait(1)
    eles = self.driver.find_elements(By.CSS_SELECTOR, "button.shopee-button-outline")
    if len(eles) > 1:
        eles[1].click()
        time.sleep(10)
    login_keywords = [
        "Đăng nhập",
        "登录",
        "Login",
        "Log in",
        "Sign in",
        "SIGN IN",
        "登入",
        "Entrar",
        "ENTRAR",
        "Iniciar sesión",
        "Masuk",
        "เข้าสู่ระบบ",
    ]
    xpath_condition = " or ".join([f"contains(normalize-space(.), '{kw}')" for kw in login_keywords])
    # 可选加 elementtiming 属性作为辅助定位
    login_btn_xpath = (
        f"//button[{xpath_condition} "
        "or @elementtiming='shopee:heroComponentPaint' "
        "or contains(@class, 'submit-btn')]"
    )

    def _is_still_login_page() -> bool:
        try:
            current_url = str(self.driver.current_url or "").lower()
        except Exception:
            current_url = ""
        login_url_tokens = (
            "account/signin",
            "accounts.shopee",
            "/seller/login",
            "/login",
            "signin",
        )
        if any(token in current_url for token in login_url_tokens):
            return True
        try:
            # Selenium 的 element.text 会遍历整棵可见 DOM。新版 Seller Centre
            # 页面很大，读取 body.text 可能让 ChromeDriver 数分钟无响应。
            body_text = str(
                self.driver.execute_script(
                    "return (document.body && document.body.innerText || '').slice(0, 8000);"
                )
                or ""
            )
        except Exception:
            body_text = ""
        lower_body = body_text.lower()
        return (
            ("central do vendedor" in lower_body and ("entrar" in lower_body or "esqueci minha senha" in lower_body))
            or ("novo na shopee" in lower_body and "cadastrar" in lower_body)
            or ("fazer login com sms" in lower_body)
        )

    try:
        self.driver.implicitly_wait(0)  # 临时取消隐式等待

        elements = self.find_elements(By.XPATH, login_btn_xpath)
        self.driver.implicitly_wait(10)  # 恢复默认隐式等待

        if not elements:
            if _is_still_login_page():
                print("当前仍是 Shopee 登录页，但未识别到可点击登录按钮。")
                return False
            print("未发现登录按钮 → 当前可能已登录。")
            try:
                WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.ID, "app")))
                print("检测到 <div id='app'> → 已登录。")

            except:
                print("未检测到登录按钮，也未发现 #app，尝试继续。")
            return True
        else:
            login_button = elements[0]
            button_text = self.driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim().slice(0, 100);",
                login_button,
            )
            print(f"检测到登录按钮 '{button_text}'，准备点击登录...")
            # 滚动到按钮并点击
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", login_button)
            login_button.click()

            print("等待登录成功中...")
            WebDriverWait(self.driver, timeout, 0.5).until(lambda _driver: not _is_still_login_page())
            print("已离开登录页 → 登录成功。")
            return True

    except Exception as e:
        print(f"check_and_login 异常: {e}")
        print(f"当前页面 URL: {self.driver.current_url}, 标题: {self.driver.title}")
        print(traceback.format_exc())
        return False

    finally:
        # 确保总是恢复默认隐式等待
        self.driver.implicitly_wait(10)

def shopee_check_and_login_version2(self,store_name,error_file,email_address,email_auth, timeout=30) -> bool:
    """
    检查是否已登录 → 如果未登录则点击登录按钮 → 等待登录成功
    登录成功判断标准：检测到 <div id='app'> 出现
    """
    try:
        self.driver.implicitly_wait(0)  # 临时取消隐式等待
        if 'account/signin'not in self.driver.current_url:
            print(f"检测到{self.driver.current_url}不是登录地址")
            return True

        # ====== 新版登录按钮定位：多条件避免失效 ======
        login_xpath = (
            "//button[contains(@class, 'submit-btn') "
            "or contains(., '登入') "
            "or contains(., '登录') "
            "or contains(., 'Đăng nhập') "
            "or contains(., 'Login')]"
        )
        login_buttons = self.driver.find_elements(By.XPATH, login_xpath)
        if not login_buttons:
            print("未发现登录按钮 → 尝试判断是否已登录")
            try:
                WebDriverWait(self.driver, 2).until(
                    EC.presence_of_element_located((By.ID, "app"))
                )
                print("检测到 <div id='app'> → 已登录")
                return True
            except:
                print("登录按钮和 app 都未找到 → 页面可能未加载完")
                return False
        # ====== 点击登录按钮 ======
        login_btn = login_buttons[0]
        print("检测到登录按钮，点击中...")
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", login_btn)
        login_btn.click()
        # ====== 登录后可能弹验证码 → 检测验证码窗口 ======
        print("已点击登录按钮，检测验证码窗口...")
        alert_otp_eles = self.driver.find_elements(By.CSS_SELECTOR, "div.eds-modal__mask")
        if len(alert_otp_eles)>0:
            print("检测到验证码弹框！")
            email = shopee_email_from_otp_modal(self)
            print(f"提取到邮箱验证码{email}")
            if email is None:
                write_txt = f"{store_name}：非邮箱验证码，验证码登录失败"
                print(write_txt)
                error_txt = read_txt(error_file)
                if store_name not in error_txt:
                   append_txt(error_file,write_txt)
                return False
            email_deal = email.replace('*','')
            get_email_type = "Shopee"
            email_prefix, email_suffix = split_email(email_deal)
            print(f"正在处理获取{email_deal}验证码。")
            sleep(30)
            otp_code, send_time = get_opt_code(email_address, email_auth, email_prefix, email_suffix, get_email_type)
            print(f"提取到验证码：{otp_code}，接收时间：{send_time}")
            res = shopee_input_email_otp_and_confirm(self,store_name,otp_code,5)
            if not res:
                return False
            else:
                return True
        else:
            if 'account/signin' not in self.driver.current_url:
                print(f"检测到{self.driver.current_url}不是登录地址")
                return True

        # ====== 等待登录成功 ======
        print("等待登录成功中...")
        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "app"))
        )

        print("检测到 <div id='app'> → 登录成功")

    except Exception as e:
        print(f"check_and_login 异常: {e}")
        print(f"当前页面 URL: {self.driver.current_url}, 标题: {self.driver.title}")
        return False

    finally:
        # 确保总是恢复默认隐式等待
        self.driver.implicitly_wait(10)


def set_switch_by_name(driver, name_text):
    """
    根据名称定位并控制对应的开关按钮

    Args:
        driver: selenium webdriver
        name_text: 这一行的名称文本，例如：Instant Delivery - ส่งทันที (แพ็ก 2 ชั่วโมง)
    """

    # 找到包含该文本的那一行
    row_xpath = f"//div[contains(normalize-space(), '{name_text}')]"
    row = driver.find_element(By.XPATH, row_xpath)

    # 在这一行中找开关按钮
    toggle = row.find_element(By.CSS_SELECTOR, "[data-testid='channel-enable-toggle']")

    # 判断当前开关状态
    cls = toggle.get_attribute("class")
    is_on = "eds-switch--open" in cls

    return is_on
