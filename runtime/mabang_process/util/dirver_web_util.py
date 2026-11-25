from time import sleep
from selenium import webdriver
from selenium.common import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import time
from selenium.webdriver.support.ui import Select
import re
import sys, os
# process_config_path = os.path.dirname(__file__)
# config_path = os.path.dirname(os.path.dirname(__file__))
# sys.path.append(config_path)



def select_warehouse(driver, warehouse_id,warehouse_name):
    """
    选中指定仓库，可以通过仓库名称或仓库ID
    :param driver: Selenium webdriver 对象
    :param warehouse_identifier: 仓库名称（str）或仓库ID（str/int）
    """
    sleep(0.5)  # 等待下拉菜单渲染
    # 尝试按 ID 查找
    try:
        warehouse_checkbox = driver.find_element(By.CSS_SELECTOR,
                                                 f'input.findWarehouse[value="{warehouse_id}"]')
        if not warehouse_checkbox.is_selected():
            driver.execute_script("arguments[0].click();", warehouse_checkbox)
        return True
    except:
        pass

    # 尝试按名称查找
    try:
        # 找到所有仓库 checkbox 元素
        warehouses = driver.find_elements(By.CSS_SELECTOR, 'input.findWarehouse')
        for w in warehouses:
            name = w.find_element(By.XPATH, './following-sibling::span').text
            if name == warehouse_name:
                if not w.is_selected():
                    driver.execute_script("arguments[0].click();", w)
                return True
    except:
        pass

    print(f"未找到仓库: {warehouse_name}")
    return False



def download_reports_getIncomePayData(driver):
    driver.get('https://900853.private.mabangerp.com/index.php?mod=reports.incomePay')
    wait_for_invisible(driver, 'div.loading_large.loading-mask')
    ul_ele = driver.find_element(By.CSS_SELECTOR, "ul.incomeUl")
    li_list = ul_ele.find_elements(By.TAG_NAME, "li")
    driver.execute_script("arguments[0].click();", li_list[7])
    wait_for_invisible(driver, 'div.loading_large.loading-mask')







def wait_for_new_window(driver, timeout=30):
    """
    等待浏览器出现新窗口，并返回新窗口的句柄。

    :param driver: Selenium WebDriver 对象
    :param timeout: 最大等待时间（秒）
    :return: 新窗口的句柄
    """
    # 等待新窗口出现
    WebDriverWait(driver, timeout).until(lambda d: len(d.window_handles) > 1)

    # 获取所有窗口句柄
    windows = driver.window_handles

    # 返回最后一个窗口（通常是新打开的窗口）
    return windows[-1]


def get_shopid_list_by_country(driver, country='菲律宾'):
    driver.get('https://900853.private.mabangerp.com/index.php?mod=reports.incomePay')
    wait_for_invisible(driver,'.loading-mask')
    # 点击切回旧版
    load_onclick(driver,"switchNewOld('reports.incomePay', 'old')")
    wait_for_invisible(driver,'.loading-mask')
    ul_ele = driver.find_element(By.CSS_SELECTOR,'ul.nav.nav-tabs.incomeUl')
    li_eles = ul_ele.find_elements(By.CSS_SELECTOR,'li')
    a = li_eles[7].find_element(By.TAG_NAME, "a")
    driver.execute_script("arguments[0].click();", a)
    # click_element_no_head(driver,By.XPATH,"//a[@data-type='shop']/..")
    wait_for_invisible(driver,'.loading-mask')
    li_eles = driver.find_elements(By.XPATH, "//li[@class='shop_class' and @data-status='1']")
    shop_id_list =[]
    for li in li_eles:
        # 店铺名
        shop_name = li.find_element(By.XPATH, ".//span[@class='text']").get_attribute("textContent").strip()
        print(shop_name)
        # 判断是否包含“菲律宾”
        if country in shop_name:
            # 获取店铺 ID（input 的 value）
            shop_id = li.find_element(By.XPATH, ".//input").get_attribute("value")
            shop_id_list.append(shop_id)
    return driver,shop_id_list

def hover_via_actionchains(driver, el):
    try:
        ActionChains(driver).move_to_element(el).perform()
        return True
    except Exception:
        return False

def hover_via_js_dispatch(driver, el):
    # dispatch mouseover + mouseenter + mousemove events
    script = """
    const el = arguments[0];
    function dispatch(type) {
      const ev = new MouseEvent(type, {bubbles: true, cancelable: true, view: window});
      el.dispatchEvent(ev);
    }
    dispatch('mouseover'); dispatch('mouseenter'); dispatch('mousemove');
    """
    try:
        driver.execute_script(script, el)
        return True
    except Exception:
        return False

def force_show_dropdown(driver):
    # 最后手段：直接把下拉dom设为可见（你之前用过类似办法）
    script = """
    const dd = document.querySelector('div.ant-dropdown');
    if(dd){
        dd.style.display = 'block';
        dd.style.visibility = 'visible';
        dd.style.opacity = '1';
    }
    """
    driver.execute_script(script)




def click_text_target(driver,text_content, target='span'):
    """
    点击元素中的文本
    :param driver:
    :param text_content: 文本内容
    :param target: 标签
    :return:
    """
    # xpath_txt = f"//{target}[normalize-space(text())='{text_content}']"
    xpath_txt = f"//{target}[contains(normalize-space(text()), '{text_content}')]"
    elem = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, xpath_txt))
    )
    # 滚动进可视区域
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)

    # 直接 JavaScript 点击（无头最稳）
    driver.execute_script("arguments[0].click();", elem)
    sleep(2)
    driver.execute_script("arguments[0].click();", elem)


def click_sync_all(driver,parent_selector, selector,parent_by=By.XPATH,by=By.XPATH):
    """
    店小秘悬浮点击
    :param driver:
    :param parent_selector: 需要悬浮的元素
    :param selector: submit提交按钮元素
    :param by: 默认为xpath
    :return:
    """
    # btn_xpath = "//button[contains(@class,'ant-btn') and .//span[normalize-space()='同步产品']]"
    # 这里必须加 wait，才能拿到真正的元素
    btn = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((parent_by, parent_selector))
    )
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
        btn
    )
    time.sleep(0.3)
    ok = hover_via_actionchains(driver, btn)
    time.sleep(0.5)

    # 尝试 2：如果 ActionChains 无效，使用 JS 派发鼠标事件
    if not ok:
        ok = hover_via_js_dispatch(driver, btn)
        time.sleep(0.5)
    try:
        # 等 menu item 可见（常规）
        wait_short = WebDriverWait(driver, 2)
        wait_short.until(EC.visibility_of_element_located(
            (by, selector)))
    except Exception:
        # 降级：强制显示下拉并再试
        force_show_dropdown(driver)
        time.sleep(0.3)
    wait = WebDriverWait(driver, 10)
    el = wait.until(EC.element_to_be_clickable((by, selector)))
    el.click()

    print("已点击：同步全部产品")

def select_page_size(driver,parent_selector,selector, timeout=10):
    try:
        # 点击下拉框
        dropdown = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((
                By.XPATH, parent_selector
            ))
        )
        dropdown.click()  # 用 Selenium 点击而非 JS
        WebDriverWait(driver, 0.5).until(lambda d: True)  # 小延时，确保选项渲染

        # 等待选项可见
        option = WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((
                By.XPATH, selector
            ))
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", option)
        ActionChains(driver).move_to_element(option).click().perform()

        print(f"成功选择分页条数")
        return True
    except Exception as e:
        print(f"选择分页条数失败: {e}")
        return False


def check_allow_by_index(driver, selector, index, isselect=True, by=By.CSS_SELECTOR):
    elements = driver.find_elements(by, selector)
    elem = elements[index]

    # 只在状态不符合期望时点击
    if elem.is_selected() != isselect:
        driver.execute_script("arguments[0].click();", elem)

def get_element_content(driver, selector, by=By.CSS_SELECTOR, timeout=30):
    """
    获取元素内的文本内容

    参数:
    - driver: WebDriver实例
    - selector: 选择器
    - by: 查找方式，默认为CSS_SELECTOR
    - timeout: 等待超时时间，默认为30秒

    返回:
    - 元素的文本内容，如果元素不存在则返回空字符串
    """
    try:
        # 使用显式等待确保元素可见
        element = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, selector))
        )
        return element.text
    except Exception as e:
        print(f"获取元素内容失败: {e}")
        return ""

#等待遮罩消失
def wait_for_invisible(driver, selector, by=By.CSS_SELECTOR, timeout=30):
    """
    等待某个元素消失或隐藏（支持 display:block → display:none）
    :param driver: WebDriver 实例
    :param selector: 元素选择器（CSS/XPath/ID...）
    :param by: 定位方式，默认 CSS_SELECTOR
    :param timeout: 超时时间，默认 30 秒
    """
    sleep(1)
    wait = WebDriverWait(driver, timeout)

    # 1. 等元素出现在 DOM 中（防止还没渲染就去检查）
    try:
        wait.until(EC.presence_of_element_located((by, selector)))
        # 2. 等元素 display:none 或被移除
        wait.until(lambda d: d.execute_script(
            f"""
            const el = document.querySelector('{selector}');
            return !el || window.getComputedStyle(el).display === 'none';
            """
        ))

    except:
        # 如果元素一直没出现，直接继续，说明不存在，也算“隐藏”
        return True
    sleep(3)

    return True









def click_element_no_head(driver, selector, by=By.CSS_SELECTOR, timeout=30):
    """
    无头点击 目标
    :param driver:
    :param selector: 目标
    :param by: 选择器
    :param timeout: 超时时间
    :return:
    """
    element = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, selector))
    )
    driver.execute_script("arguments[0].click();", element)


def select_page_by_bs(driver,selector,page,by=By.CSS_SELECTOR):
    # 找到 select 元素
    select_elem = driver.find_element(by, selector)

    # 创建 Select 对象
    select = Select(select_elem)

    # 选择 300
    select.select_by_value(page)


def clean_purchase_plan(driver,op_name):
    """
    清空采购计划
    :param driver:
    :param op_name: 操作人
    :return:
    """
    clean_data = {}
    purchase_url = "https://900853.private.mabangerp.com/index.php?mod=purchasenew.myapplylist"
    driver.get(purchase_url)
    print(f'刷新网页：{purchase_url}')
    wait_for_invisible(driver, 'div.loading_large.loading-mask')
    """等待选择未采购"""
    is_block,not_purchase = wait_element_block(driver,"//li[contains(@class,'stock-search-form-click')]/a[text()='未采购']",By.XPATH)
    if not is_block:
        clean_data = {"status":False,"msg":"采购计划网页加载超时"}
        return driver,clean_data
    # 用 JS 点击，避免无头模式 click 失效
    driver.execute_script("arguments[0].click();", not_purchase)

    wait_for_invisible(driver, 'div.loading_large.loading-mask')
    """等待申请人按钮出现"""
    is_block, applicant_radio = wait_element_block(driver,"//input[@type='radio' and @name='searchType' and @value='createOperName']",By.XPATH)
    # 用 JS 点击，保证无头模式稳定
    driver.execute_script("arguments[0].click();", applicant_radio)
    print("已选中 申请人 单选按钮")
    is_block, search_input = wait_element_block(driver,"search-content-text",By.ID)
    # 清空原有内容
    search_input.clear()
    # 输入关键字
    search_input.send_keys(op_name)
    time.sleep(2)
    driver.set_script_timeout(900)
    driver.execute_script("getPaginationData(1,500)")
    wait_for_invisible(driver, 'div.loading_large.loading-mask')    # purchase_url = "https://900853.private.mabangerp.com/index.php?mod=purchasenew.myapplylist"

    is_block, notdatya_ele = wait_element_block(driver, "div.alert-nodata", By.CSS_SELECTOR)
    if is_block and notdatya_ele.is_displayed():
        clean_data = {"status": True, "msg": "没有需要清空的数据"}
        return driver,clean_data
    is_block, select_input = wait_element_block(driver, "checkall", By.ID)
    if not is_block:
        clean_data = {"status": False, "msg": "未找到复选框"}
        return driver,clean_data
    driver.execute_script("arguments[0].click();", select_input)

    click_element_no_head(driver,"//button[@onclick='batchHandlePurchaseRequestDetailShow()']",By.XPATH)
    sleep(2)

    click_element_no_head(driver,"input[name='handType'][value='delete']")

    write_content_element(driver,"异常计划","reason",By.NAME)
    sleep(2)
    load_onclick(driver, 'handlePurchaseRequestDetail()')
    clean_data = {"status": True, "msg": "异常计划清除完毕"}
    return driver,clean_data


def import_purchase_items(driver,data,op_name):
    """
    采购商品导入
    :param driver: WebDriver 实例
    :param data: 导入文件路径
    :return driver 实例
    """
    purchase_url = "https://900853.private.mabangerp.com/index.php?mod=purchasenew.myapplylist"
    driver.get(purchase_url)
    print(f'跳转至 采购计划：{purchase_url}')
    print(f'导入数据：{data}')
    wait_for_invisible(driver, 'div.loading_large.loading-mask')
    # 点击确定按钮
    load_onclick(driver,"addpurchasedetailshow();")
    is_block, textarea = wait_element_block(driver, "purchaseTextarea", By.ID)
    textarea.send_keys(data)
    sleep(3)
    driver.set_script_timeout(900)
    print(f'执行上传按钮：addPurchaseRequestFieldsave();)')
    # 点击确定按钮
    driver.execute_script("addPurchaseRequestFieldsave();")
    print(f'操作员：{op_name} 上传数据成功！')
    wait_for_invisible(driver, 'div.loading_large.loading-mask')

    return driver






def click_element_look_no_head(driver, selector, by=By.CSS_SELECTOR, timeout=15):
    """
    无头点击 目标
    :param driver:
    :param selector: 目标
    :param by: 选择器
    :param timeout: 超时时间
    :return:
    """
    buttons = driver.find_elements(by, selector)
    for btn in buttons:
        if btn.is_displayed():  # 只选择可见的
            btn.click()
            break

def submit_btn_no_head(driver, selector, by=By.CSS_SELECTOR):
    """
    点击提交按钮
    :param driver:
    :param selector: 目标
    :param by: 选择器
    :return:
    """
    submit_btn = driver.find_element(by, selector)
    driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", submit_btn)

def wait_element_block(driver,selector, by=By.CSS_SELECTOR, timeout=30):
    """
    等待元素出现
    :param driver:
    :param selector: 选择器内容
    :param by: 选择器
    :param timeout: 超时时间
    :return: 是否出现|元素目标
    """
    is_block = False
    p_element = None
    try:

        p_element = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, selector))
        )
        is_block = True
    except TimeoutException:
        print("当前元素不存在")
    return is_block,p_element



def switch_iframe(driver,selector,by=By.ID):
    """
    切换到iframe里面
    :param driver:
    :param selector:
    :param by:
    :return:
    """
    WebDriverWait(driver, 20).until(
        EC.frame_to_be_available_and_switch_to_it((by, selector))
    )


def write_textarea_nohead(driver,selector,write_str,by=By.CSS_SELECTOR):
    textarea = driver.find_element(by, selector)
    textarea.clear()
    driver.execute_script("""
    arguments[0].value = arguments[1];
    arguments[0].dispatchEvent(new Event('input'));
    arguments[0].dispatchEvent(new Event('change'));
    """, textarea, write_str)

def double_click_input(driver,selector,by=By.CSS_SELECTOR):
    """
    双击INput
    :param driver:
    :param selector:
    :param by:
    :return:
    """
    input_box = driver.find_element(by, selector)
    ActionChains(driver).double_click(input_box).perform()


def get_button_disabled(driver,selector,by=By.CSS_SELECTOR):
    button = driver.find_element(by, selector)
    disabled = button.get_attribute("disabled")
    if disabled is not None:
        return False
    else:
        return True

def write_content_element(driver,content,selector, by=By.CSS_SELECTOR):
    """
    在元素中输入
    :param driver:
    :param content: 填写内容
    :return:
    """
    input_element = driver.find_element(by, selector)  # 清空原来的内容（如果有）
    input_element.clear()
    input_element.send_keys(str(content))
    input_element.send_keys(Keys.TAB)  # 或 Keys.ENTER

def check_input_no_head(driver, selector, by=By.CSS_SELECTOR, timeout=15):
    """
    选择复选框
    :param driver:
    :param selector: 目标
    :param by: 选择器
    :param timeout: 超时时间
    :return:
    """
    checkbox = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, selector))
    )
    # 判断是否已选中
    is_checked = checkbox.is_selected()
    if not is_checked:
        driver.execute_script("arguments[0].click();", checkbox)
def not_select_input_no_head(driver, selector, by=By.CSS_SELECTOR, timeout=15):
    """
    选择复选框
    :param driver:
    :param selector: 目标
    :param by: 选择器
    :param timeout: 超时时间
    :return:
    """
    checkbox = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, selector))
    )
    # 判断是否已选中
    is_checked = checkbox.is_selected()
    if is_checked:
        driver.execute_script("arguments[0].click();", checkbox)

def load_onclick(driver,onclick_content):
    """
    直接调用 onclick函数
    :param driver:
    :param onclick_content:
    :return:
    """
    driver.execute_script(onclick_content)
    sleep(1)


def select_shops_by_country(driver,country,container_selector,lable_selector,container_by=By.ID,lable_by=By.TAG_NAME):
    """
    根据国家选中所有店铺名称中包含某国复选框（精确找 <span class="text">）
    :param driver:
    :param country: 国家
    :param container_selector: 容器选择器内容
    :param lable_selector:  复选框选择器内容
    :param container_by: 容器选择器类型
    :param lable_by: 复选框选择器类型
    :return:
    """
    try:
        # 等待店铺容器加载
        wait = WebDriverWait(driver, 5)
        container = wait.until(
            EC.presence_of_element_located((container_by, container_selector))
        )
        # 找到所有 li 元素
        shop_items = container.find_elements(By.TAG_NAME, "li")
        count = 0
        for li in shop_items:
            try:
                # 找 label 下的 span.text
                label = li.find_element(lable_by, lable_selector)
                spans = label.find_elements(By.CSS_SELECTOR, "span.text")

                if spans:
                    shop_name = spans[0].get_attribute("innerText").strip()
                else:
                    # 没有 span.text 就用 label 内部文本
                    shop_name = label.get_attribute("innerText").strip()
                if not shop_name:
                    continue  # 为空跳过

                # 如果店铺名包含指定国家，就选中复选框
                if country in shop_name:
                    print(f"选择店铺：{shop_name}")
                    driver.execute_script("arguments[0].click();", label)
                    count += 1
            except Exception as e_inner:
                print("单个 li 处理失败:", e_inner)

        print(f"成功选中 {count} 个包含'{country}'的店铺")

    except Exception as e:
        print("选择店铺失败:", e)





def get_dirver_web(chrome_path, chrome_driver_path, no_head=True, download_path=None):
    if not download_path:
        raise ValueError("download_path is required")
    options = webdriver.ChromeOptions()
    options.binary_location = chrome_path
    # options.add_argument("--disable-features=BeforeUnload")  # 禁止 beforeunload 离开页面弹框（核心）
    # ---------- 无头模式 + 下载路径设置 ----------
    if no_head:
        options.add_argument("--headless=new")  # 新无头模式
        options.add_argument("--window-size=1920,1080")  # 保证元素可见
    # 设置下载路径，下载自动完成
    prefs = {
        "download.default_directory": download_path,  # 下载目录
        "download.prompt_for_download": False,  # 不弹出下载框
        "profile.default_content_setting_values.automatic_downloads": 1
    }
    options.add_experimental_option("prefs", prefs)
    service = Service(chrome_driver_path)
    driver = webdriver.Chrome(service=service, options=options)

    return driver


def double_click_element(driver, element, scroll_into_view=True):
    """
    在浏览器中双击指定元素，兼容无头模式。

    :param driver: Selenium WebDriver 对象
    :param element: Selenium WebElement 对象
    :param scroll_into_view: 是否滚动到元素可见位置
    """
    if scroll_into_view:
        driver.execute_script("arguments[0].scrollIntoView(true);", element)
        time.sleep(0.2)  # 滚动完成稍微等待一下

    # 用 JS 触发 double click 事件
    driver.execute_script("""
    var evt = new MouseEvent('dblclick', {
        bubbles: true,
        cancelable: true,
        view: window
    });
    arguments[0].dispatchEvent(evt);
    """, element)
from typing import Union, Tuple, Optional
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.remote.webdriver import WebDriver
def wait_for_new_window1(
        driver: WebDriver,
        old_handles: set,
        timeout: int = 30,
        poll_interval: float = 0.5,
        wait_for_selector: Optional[Union[str, Tuple[str, str]]] = None
) -> Tuple[bool, Optional[str]]:
    """
    等待新窗口出现并自动切换，页面加载完成。

    参数:
        driver: Selenium WebDriver 实例
        old_handles: 点击前已有窗口句柄集合
        timeout: 最大等待时间（秒）
        poll_interval: 轮询间隔（秒）
        wait_for_selector: 可选，等待页面关键元素出现
            - CSS Selector 字符串，例如 "#submit-button"
            - 元组 (By.ID, "element_id") 或 (By.CSS_SELECTOR, "selector")

    返回:
        (has_new_window, new_window_handle)
        - has_new_window: True/False 是否检测到新窗口
        - new_window_handle: 新窗口句柄，若没有则为 None
    """
    start = time.time()
    while time.time() - start < timeout:
        new_handles = set(driver.window_handles) - old_handles
        if new_handles:
            new_window = list(new_handles)[0]
            driver.switch_to.window(new_window)

            # 等待页面 DOM 加载完成
            try:
                WebDriverWait(driver, timeout).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except TimeoutException:
                pass  # 超时也不报错

            # 如果指定了关键元素，继续等待该元素出现
            if wait_for_selector:
                try:
                    if isinstance(wait_for_selector, tuple):
                        WebDriverWait(driver, timeout).until(
                            EC.presence_of_element_located(wait_for_selector)
                        )
                    else:
                        WebDriverWait(driver, timeout).until(
                            EC.presence_of_element_located(("css selector", wait_for_selector))
                        )
                except TimeoutException:
                    pass  # 超时也不报错

            return True, new_window

        time.sleep(poll_interval)

    return False, None


def check_mabang_login(driver):
    """验证是否登录成功"""
    try:
        nickname = driver.find_element(By.CSS_SELECTOR, "#nickname").text.strip()
        print("马帮登录成功！用户：", nickname)
        return True
    except:
        print("登录失败")
        return False

def login_mabang(driver,uname,pwd,LOGIN_URL="https://900853.private.mabangerp.com/index.htm"):
    print(f"正在打开马帮ERP：{LOGIN_URL}")
    # ---------- 打开登录页 ----------
    driver.get(LOGIN_URL)
    print(f"输入账号：{uname}")
    # ---------- 2. 输入账号密码 ----------
    driver.find_element(By.ID, "username").send_keys(uname)
    print(f"输入密码：*******")
    driver.find_element(By.ID, "password").send_keys(pwd)
    driver.find_element(By.ID, "login-but").send_keys(Keys.RETURN)
    print(f"正在登录中.....")
    # ---------- 3. 等待登录完成 ----------
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".nav-hd"))
    )
    cookies = driver.get_cookies()
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    print("Cookie 获取成功")
    return driver,cookie_str
