# util_dxm.py
from selenium.webdriver.common.by import By
from time import sleep
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

class DXMHelper:
    def __init__(self):
        self.base_url = "https://www.dianxiaomi.com/web/shopeeSite/online"

    # wait helper (依赖你现有 dirver_web_util.wait_for_invisible)
    def wait_for_invisible(self, driver, id_name, by=By.ID, timeout=30):
        try:
            from util import dirver_web_util
            dirver_web_util.wait_for_invisible(driver, id_name, by)
        except Exception:
            # fallback 简单 sleep
            sleep(1)

    def click_shop_by_text(self, driver, dp_text):
        """尝试点击店铺名，返回 True/False"""
        try:
            elems = driver.find_elements(By.XPATH, f"//span[contains(normalize-space(text()), '{dp_text}')]")
            if not elems:
                return False
            # prefer visible one
            for e in elems:
                try:
                    driver.execute_script("arguments[0].scrollIntoView(true);", e)
                    driver.execute_script("arguments[0].click();", e)
                    return True
                except Exception:
                    continue
            # fallback click first
            driver.execute_script("arguments[0].click();", elems[0])
            return True
        except Exception:
            return False

    def has_pager(self, driver):
        try:
            elems = driver.find_elements(By.XPATH, "//div[contains(@class,'vxe-pager')]")
            return len(elems) > 0
        except Exception:
            return False

    def get_page_count(self, driver):
        try:
            pager_container = driver.find_element(By.XPATH, "//div[contains(@class,'vxe-pager')]")
            spans = pager_container.find_elements(By.TAG_NAME, "span")
            for span in spans:
                text = span.text.strip()
                if text.isdigit():
                    return int(text)
            return 1
        except Exception:
            return 1

    def click_next_page(self, driver):
        try:
            btn = driver.find_element(By.XPATH, "//button[contains(@class,'vxe-pager--next-btn')]")
            driver.execute_script("arguments[0].click();", btn)
            return True
        except Exception:
            return False

    def select_first_checkbox(self, driver, dp):
        # 选中前两个 input（你原逻辑）
        try:
            input_elements = driver.find_elements(By.CSS_SELECTOR, "input.ant-checkbox-input")
            if len(input_elements) < 1:
                return False
            if not input_elements[0].is_selected():
                driver.execute_script("arguments[0].click();", input_elements[0])
            return True
        except Exception:
            return False

    def click_batch_fullattribute(self, driver):
        # 点击 批量操作 -> 全属性修改
        try:
            from util import dirver_web_util
            dirver_web_util.click_sync_all(driver,
                                           "//button[contains(@class,'ant-btn') and .//span[normalize-space()='批量操作']]",
                                           "//div[contains(@class,'menu-item') and normalize-space()='全属性修改']")
            return True
        except Exception:
            return False

    def check_input(self, driver, xpath, by=By.XPATH):
        try:
            from util import dirver_web_util
            dirver_web_util.check_input_no_head(driver, xpath, by)
            return True
        except Exception:
            # fallback: try click label
            try:
                el = driver.find_element(by, xpath)
                driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    def click_element(self, driver, xpath, by=By.XPATH):
        try:
            from util import dirver_web_util
            dirver_web_util.click_element_no_head(driver, xpath, by)
            return True
        except Exception:
            try:
                el = driver.find_element(by, xpath)
                driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    def is_sync_finished(self, driver):
        # 返回 True 表示完成
        try:
            ele = driver.find_element(By.ID, 'commonProgressBarStatus')
            text = ele.text or ""
            return '进行中' not in text
        except NoSuchElementException:
            return True
        except StaleElementReferenceException:
            return False
        except Exception:
            return True

    def get_progress_detail_html(self, driver):
        try:
            ele = driver.find_element(By.ID, "commonProgressBarDetail")
            return ele.get_attribute("innerHTML")
        except Exception:
            return ""

    def parse_sync_failed(self, html):
        try:
            from util import mabang_html_parse as ps
            return ps.parse_sync_failed(html)
        except Exception:
            return []

    def close_dialogs(self, driver):
        try:
            close_btns = driver.find_elements(By.XPATH, "//button[span[text()='关闭']]")
            for b in close_btns:
                try:
                    driver.execute_script("arguments[0].click();", b)
                except:
                    pass
        except Exception:
            pass

    def exists(self, driver, xpath):
        try:
            return len(driver.find_elements(By.XPATH, xpath)) > 0
        except Exception:
            return False
