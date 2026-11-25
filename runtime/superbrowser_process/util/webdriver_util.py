import logging
import time
import traceback
from typing import List, Literal

from selenium.common import StaleElementReferenceException, WebDriverException, InvalidSessionIdException, \
    NoSuchWindowException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait

from config import config


class WebdriverUtil(object):

    def __init__(self, driver):
        self.driver = driver
        self.close_all_tab()
        self.driver.set_page_load_timeout(config.get_page_load_timeout())
        self.driver.set_script_timeout(config.get_page_load_timeout())

    def go_to(self, url, timeout=30):
        """
        打开指定url，设置指定超时时间，打开后设置回原来的超时时间
        :param url: 目标地址
        :param timeout: 超时时间
        :return:
        """
        implicit_wait = self.driver.timeouts.implicit_wait
        self.driver.implicitly_wait(timeout)
        try:
            self.driver.get(url)
        finally:
            # 页面加载超时也必须恢复，否则后续候选元素探测会反复卡住 timeout 秒。
            self.driver.implicitly_wait(implicit_wait)

    def close_all_tab(self) -> None:
        """
        ?????????????
        """
        try:
            handle_list = list(self.driver.window_handles)
        except (WebDriverException, InvalidSessionIdException):
            logging.warning("跳过标签页清理，webdriver 会话已断开")
            return

        for handle in handle_list:
            try:
                self.driver.switch_to.window(handle)
                current_url = self.driver.current_url
            except (WebDriverException, InvalidSessionIdException, NoSuchWindowException):
                logging.warning("清理过程中跳过不可用标签页", exc_info=True)
                continue
            if current_url.startswith("devtools://"):
                continue
            return

        logging.warning("webdriver 初始化时未找到可用标签页")
    def switch_to_latest_tab(self) -> str:
        """
        切换到最新打开的标签页
        :return: 原始窗口句柄
        """
        original_handle = self.driver.current_window_handle
        handles = self.driver.window_handles
        latest_handle = handles[-1]  # 列表最后一个就是最新打开的
        self.driver.switch_to.window(latest_handle)
        return original_handle

    def switch_tab(self, origin_handle=None, origin_url=None) -> str:
        if not origin_handle:
            origin_handle = self.driver.current_window_handle
        if not origin_url:
            origin_url = self.driver.current_url
        handles = self.driver.window_handles
        for item in handles:
            self.driver.switch_to.window(item)
            try:
                current_url = self.driver.current_url
            except Exception:
                logging.info("切换标签页加载超时...")
                return origin_handle
            if current_url != origin_url:
                break
        return origin_handle
    def find_element(self, by=By.ID, value=None) -> WebElement:
        return self.driver.find_element(by, value)

    def find_element_wait_under_element(self, element: WebElement, by=By.ID, value=None, timeout=30) -> WebElement:
        implicit_wait = self.driver.timeouts.implicit_wait
        self.driver.implicitly_wait(0)
        element = WebDriverWait(element, timeout, 0.5).until(
            expected_conditions.presence_of_element_located((by, value)))
        self.driver.implicitly_wait(implicit_wait)
        return element

    def find_element_wait(self, by=By.ID, value=None, timeout=30) -> WebElement:
        implicit_wait = self.driver.timeouts.implicit_wait
        self.driver.implicitly_wait(0)
        try:
            element = WebDriverWait(self.driver, timeout, 0.5).until(
                expected_conditions.presence_of_element_located((by, value)))
        except StaleElementReferenceException as e:
            print('StaleElementReferenceException异常，重新获取元素')
            element = WebDriverWait(self.driver, timeout, 0.5).until(
                expected_conditions.presence_of_element_located((by, value)))
        self.driver.implicitly_wait(implicit_wait)
        return element

    def find_element_wait_not(self, by=By.ID, value=None, timeout=30) -> WebElement | Literal[True]:
        implicit_wait = self.driver.timeouts.implicit_wait
        self.driver.implicitly_wait(0)
        element = WebDriverWait(self.driver, timeout, 0.5).until_not(
            expected_conditions.presence_of_element_located((by, value)))
        self.driver.implicitly_wait(implicit_wait)
        return element

    def wait_for_invisible(self,by=By.CSS_SELECTOR, value=None,  timeout=30):
        time.sleep(1)
        wait = WebDriverWait(self.driver, timeout)

        # 1. 等元素出现在 DOM 中（防止还没渲染就去检查）
        try:
            wait.until(expected_conditions.presence_of_element_located((by, value)))
            # 2. 等元素 display:none 或被移除
            wait.until(lambda d: d.execute_script(
                f"""
                const el = document.querySelector('{value}');
                return !el || window.getComputedStyle(el).display === 'none';
                """
            ))

        except:
            # 如果元素一直没出现，直接继续，说明不存在，也算“隐藏”
            return True
        time.sleep(8)
        return True

    def find_elements(self, by=By.ID, value=None) -> List[WebElement]:
        return self.driver.find_elements(by, value)

    def find_elements_wait(self, by=By.ID, value=None, timeout=30) -> List[WebElement]:
        implicit_wait = self.driver.timeouts.implicit_wait
        self.driver.implicitly_wait(0)
        elements = WebDriverWait(self.driver, timeout, 0.5).until(
            expected_conditions.presence_of_all_elements_located((by, value)))
        self.driver.implicitly_wait(implicit_wait)
        return elements

    @staticmethod
    def input_verbatim(element: WebElement, data: str):
        for character in list(data):
            element.send_keys(character)

    def move_to_by_js(self, element):
        self.driver.execute_script("arguments[0].scrollIntoView();", element)

    def move_to_click(self, element, move=True) -> None:
        """
        移动到指定元素点击
        :param element: 元素
        :param move: 是否先移动鼠标到元素上
        :return:
        """
        enable_check_count = 0
        while True:
            if enable_check_count == 60:
                break
            enabled = element.is_displayed()
            if enabled:
                break
            time.sleep(1)
            enable_check_count = enable_check_count + 1
        if move:
            ActionChains(self.driver).move_to_element(element).perform()
        try:
            self.driver.execute_script(
                """
                const el = arguments[0];
                if (typeof el.click === 'function') {
                    el.click();
                    return;
                }
                el.dispatchEvent(new MouseEvent('click', {
                    view: window,
                    bubbles: true,
                    cancelable: true
                }));
                """,
                element,
            )
        except Exception:
            element.click()

    def move_to_send_key(self, input_element, input_value: str, move=True, init_input=False) -> None:
        """
        移动到指定元素点击，并输入内容
        :param input_element: 元素
        :param input_value: 输入的内容
        :param move: 是否移动鼠标至元素
        :param init_input: 是否初始化输入框空字符串（比特保存账号密码时会导致填充无法覆盖，需要输入空字符后再清空）
        :return:
        """
        self.move_to_click(input_element, move=move)
        try:
            # input_element.click()
            if init_input:
                self.input_verbatim(input_element, " ")
            input_element.clear()
            self.input_verbatim(input_element, input_value)
        except Exception as e:
            # traceback.print_exc()
            logging.error(traceback.format_exc())
            self.driver.execute_script('arguments[0].click();', input_element)
            input_element.clear()
            self.input_verbatim(input_element, input_value)

    def quit(self) -> None:
        """
        关闭浏览器
        """
        self.driver.quit()
