import platform
import os
import time
import subprocess
import threading
import json
import requests
import uuid
import shutil
import tempfile
import zipfile
from selenium import webdriver
from selenium.common import NoSuchElementException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from concurrent.futures import ThreadPoolExecutor
from functools import partial


def _safe_response_summary(response):
    """Return an allowlisted summary safe for logs archived by HQYL."""
    if not isinstance(response, dict):
        return {"response_type": type(response).__name__}
    summary = {
        key: response.get(key)
        for key in ("statusCode", "requestId", "core_type", "core_version")
        if response.get(key) is not None
    }
    browser_list = response.get("browserList")
    if isinstance(browser_list, list):
        summary["browser_count"] = len(browser_list)
    return summary


class LoadSuperBrowser:
    def __init__(self, func_dict,pcfg,cfg,log):
        self.is_windows = platform.system() == 'Windows'
        self.is_mac = platform.system() == 'Darwin'
        self.browser_config = cfg['ziniao']['browser']
        self.user_config = pcfg['ziniao']['user_info']
        self.agent_config = pcfg['dingtalk']['agent']
        self.task_list = pcfg['task']
        self.driver_folder_path = self.browser_config['webdriver_path']  # 驱动文件保存路径
        self.client_path = self.browser_config['client_path']  # 浏览器客户端路径
        self.socket_port = self.browser_config['socket_port']  # 系统未被占用的端口
        self.user_info = {"company": self.user_config['company'],"username": self.user_config['username'],"password": self.user_config['password']}
        self.version = self.browser_config['version']
        self.func_dict = func_dict
        self.log = log
        try:
            self.max_threads = max(int(pcfg.get('thread_num', 1) or 1), 1)
        except (TypeError, ValueError):
            self.max_threads = 1
        self.operator_config = pcfg.get('operator', {}) or {}
        self._driver_download_lock = threading.Lock()

    def load_super_browser(self):
        if not self.client_path or not os.path.isfile(self.client_path):
            raise FileNotFoundError("紫鸟客户端路径无效，请选择现有的客户端程序文件")
        self.log.info('======关闭紫鸟所有进程======')
        self.kill_process()
        self.log.info('======下载最新紫鸟驱动======')
        self.download_driver()
        self.log.info('======启动客户端======')
        self.start_browser()
        self.log.info("======更新内核======")
        self.update_core()
        self.log.info("======获取店铺列表======")
        browser_list = self.get_browser_list()
        if not browser_list:
            self.log.warning("======获取店铺列表为空======")
        return browser_list

    def kill_process(self):
        """
        终止紫鸟客户端已启动的进程
        """
        if self.version == "v5":
            process_name = 'SuperBrowser.exe'
        else:
            process_name = 'ziniao.exe'
        if True:
            if self.is_windows:
                os.system('taskkill /f /t /im ' + process_name)
            elif self.is_mac:
                os.system('killall ziniao')
                time.sleep(3)

    def download_driver(self):
        import util.yingdao_util as yd
        import requests
        import json
        if self.is_windows:
            config_url = "https://cdn-superbrowser-attachment.ziniao.com/webdriver/exe_32/config.json"
        elif self.is_mac:
            arch = platform.machine()
            if arch == 'x86_64':
                config_url = "https://cdn-superbrowser-attachment.ziniao.com/webdriver/mac/x64/config.json"
            elif arch == 'arm64':
                config_url = "https://cdn-superbrowser-attachment.ziniao.com/webdriver/mac/arm64/config.json"
            else:
                return
        else:
            return
        response = requests.get(config_url)
        # 检查请求是否成功
        if response.status_code == 200:
            # 获取文本内容
            txt_content = response.text
            config = json.loads(txt_content)
        else:
            self.log.info(f"下载驱动失败，状态码：{response.status_code}")
            raise RuntimeError("operation aborted")
        if not os.path.exists(self.driver_folder_path):
            os.makedirs(self.driver_folder_path)

        for item in config:
            filename = item['name']
            if self.is_windows:
                filename = filename + ".exe"
            local_file_path = os.path.join(self.driver_folder_path, filename)
            if os.path.exists(local_file_path):
                self.log.info(f"驱动{filename}已存在，直接跳过下载")
                continue

            self.log.info(f"驱动{filename}不存在，开始下载...")
            yd.download_file(item['url'], local_file_path)
            if self.is_mac:
                cmd = ['chmod', '+x', local_file_path]
                subprocess.Popen(cmd)

    def _chrome_driver_filename(self, major):
        if self.is_windows:
            return 'chromedriver%s.exe' % major
        return 'chromedriver%s' % major

    def _chrome_for_testing_platforms(self):
        if self.is_windows:
            return ["win64", "win32"]
        if self.is_mac:
            arch = platform.machine()
            if arch == "arm64":
                return ["mac-arm64"]
            return ["mac-x64"]
        return ["linux64"]

    def _get_chrome_for_testing_driver_url(self, major):
        index_url = "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json"
        response = requests.get(index_url, timeout=30)
        response.raise_for_status()
        payload = response.json()
        milestone = (payload.get("milestones") or {}).get(str(major)) or {}
        downloads = (milestone.get("downloads") or {}).get("chromedriver") or []
        platforms = self._chrome_for_testing_platforms()
        for platform_name in platforms:
            for item in downloads:
                if item.get("platform") == platform_name and item.get("url"):
                    return item["url"], milestone.get("version") or ""
        raise RuntimeError("Chrome for Testing has no chromedriver for milestone %s" % major)

    def _download_chrome_for_testing_driver(self, major, target_path):
        url, version = self._get_chrome_for_testing_driver_url(major)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "chromedriver.zip")
            self.log.info("download chromedriver%s from Chrome for Testing %s" % (major, version))
            with requests.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            extract_dir = os.path.join(temp_dir, "extract")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            source_path = ""
            executable_name = "chromedriver.exe" if self.is_windows else "chromedriver"
            for root, _, files in os.walk(extract_dir):
                if executable_name in files:
                    source_path = os.path.join(root, executable_name)
                    break
            if not source_path:
                raise RuntimeError("chromedriver executable not found in downloaded archive")
            shutil.copy2(source_path, target_path)
            if not self.is_windows:
                os.chmod(target_path, 0o755)

        if not os.path.isfile(target_path):
            raise RuntimeError("chromedriver download did not create %s" % target_path)
        self.log.info("chromedriver%s saved: %s" % (major, target_path))

    def _ensure_chrome_driver(self, major):
        chrome_driver_path = os.path.join(self.driver_folder_path, self._chrome_driver_filename(major))
        if os.path.isfile(chrome_driver_path):
            return chrome_driver_path

        with self._driver_download_lock:
            if os.path.isfile(chrome_driver_path):
                return chrome_driver_path
            self.log.info("missing chromedriver%s, try downloading driver config again" % major)
            try:
                self.download_driver()
            except Exception as exc:
                self.log.warning("ziniao chromedriver download failed: %s" % exc)
            if os.path.isfile(chrome_driver_path):
                return chrome_driver_path
            try:
                self._download_chrome_for_testing_driver(major, chrome_driver_path)
            except Exception as exc:
                raise RuntimeError(
                    "missing chromedriver%s for browser core %s; expected path: %s; auto download failed: %s"
                    % (major, major, chrome_driver_path, exc)
                ) from exc
            return chrome_driver_path

    def start_browser(self):
        """
        启动客户端
        :return:
        """
        try:
            if self.is_windows:
                cmd = [self.client_path, '--run_type=web_driver', '--ipc_type=http', '--port=' + str(self.socket_port)]
            elif self.is_mac:
                cmd = ['open', '-a', self.client_path, '--args', '--run_type=web_driver', '--ipc_type=http',
                       '--port=' + str(self.socket_port)]
            else:
                raise RuntimeError("operation aborted")
            subprocess.Popen(cmd)
            time.sleep(5)
        except Exception as exc:
            self.log.error(
                "start browser process failed: error_type=%s",
                type(exc).__name__,
            )
            raise RuntimeError("operation aborted")
    def send_http(self, data, timeout=120):
        """
        通讯方式
        :param data:
        :return:
        """
        action = str(data.get("action") or "unknown")
        request_id = str(data.get("requestId") or "")
        try:
            url = 'http://127.0.0.1:{}'.format(self.socket_port)
            response = requests.post(url, json.dumps(data).encode('utf-8'), timeout=timeout)
            response.encoding = 'utf-8'
            return json.loads(response.text)
        except Exception as err:
            self.log.error(
                "%s 请求失败（requestId=%s，超时=%s秒）：%s",
                action,
                request_id,
                timeout,
                err,
            )
            return None

    def update_core(self):
        """
        下载所有内核，打开店铺前调用，需客户端版本5.285.7以上
        因为http有超时时间，所以这个action适合循环调用，直到返回成功
        """
        data = {
            "action": "updateCore",
            "requestId": str(uuid.uuid4()),
        }
        data.update(self.user_info)
        while True:
            result = self.send_http(data)
            self.log.info("updateCore response: %s", _safe_response_summary(result))
            if result is None:
                self.log.info("等待客户端启动...")
                time.sleep(2)
                continue
            if result.get("statusCode") is None or result.get("statusCode") == -10003:
                self.log.info("当前版本不支持此接口，请升级客户端")
                return
            elif result.get("statusCode") == 0:
                self.log.info("更新内核完成")
                return
            else:
                self.log.info("等待更新内核: %s", _safe_response_summary(result))
                time.sleep(2)

    def get_browser_list(self) -> list:
        """
        获取店铺列表
        :return:
        """
        request_id = str(uuid.uuid4())
        data = {
            "action": "getBrowserList",
            "requestId": request_id
        }
        data.update(self.user_info)

        r = self.send_http(data)
        if str(r.get("statusCode")) == "0":
            browser_list = r.get("browserList") or []
            self.log.info(
                "getBrowserList success: requestId=%s count=%s",
                r.get("requestId"),
                len(browser_list),
            )
            return browser_list
        elif str(r.get("statusCode")) == "-10003":
            self.log.error("getBrowserList login error: %s", _safe_response_summary(r))
            raise RuntimeError("operation aborted")
        else:
            self.log.error("getBrowserList failed: %s", _safe_response_summary(r))
            raise RuntimeError("operation aborted")
    def get_driver(self,open_ret_json):
        """
        获取webdriver
        :param open_ret_json:
        :return:
        """
        core_type = open_ret_json.get('core_type')
        if core_type == 'Chromium' or core_type == 0:
            major = open_ret_json.get('core_version').split('.')[0]
            chrome_driver_path = self._ensure_chrome_driver(major)
            self.log.info("ChromeDriver ready for core major=%s", major)
            port = open_ret_json.get('debuggingPort')
            options = webdriver.ChromeOptions()
            options.add_argument('--log-level=3')
            options.add_experimental_option("debuggerAddress", '127.0.0.1:' + str(port))
            return self._create_chrome_driver_with_timeout(chrome_driver_path, options, port)
        else:
            return None

    def _create_chrome_driver_with_timeout(self, chrome_driver_path, options, port, timeout=35):
        result = {}

        def _create():
            try:
                result["driver"] = webdriver.Chrome(service=Service(chrome_driver_path), options=options)
            except Exception as exc:
                result["error"] = exc

        self.log.info(f"connect Selenium to debugger port: 127.0.0.1:{port}")
        worker = threading.Thread(target=_create, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise RuntimeError(f"webdriver attach timeout after {timeout}s, debuggerPort={port}")
        if result.get("error"):
            self.log.error(
                "webdriver attach failed: error_type=%s",
                type(result["error"]).__name__,
            )
            raise result["error"]
        return result.get("driver")

    def open_ip_check(self,driver, ip_check_url):
        """
        打开ip检测页检测ip是否正常
        :param driver: driver实例
        :param ip_check_url ip检测页地址
        :return 检测结果
        """
        try:
            driver.get(ip_check_url)
            driver.find_element(By.XPATH, '//button[contains(@class, "styles_btn--success")]')
            return True
        except NoSuchElementException:
            self.log.info("未找到ip检测成功元素")
            return False
        except Exception as exc:
            self.log.error("ip检测异常: error_type=%s", type(exc).__name__)
            return False

    def open_launcher_page(self,driver, launcher_page):
        try:
            driver.get(launcher_page)
        except Exception as exc:
            self.log.warning(
                "launcher page load timeout or failed: error_type=%s",
                type(exc).__name__,
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(6)

    def open_portal_path(self, driver, launcher_page, path):
        if not path:
            self.open_launcher_page(driver, launcher_page)
            return
        base = str(launcher_page or "").rstrip("/")
        if "/portal/" in base:
            base = base.split("/portal/", 1)[0]
        if not path.startswith("/"):
            path = "/" + path
        target_url = base + path
        self.log.info("open target portal path: %s", path)
        try:
            driver.get(target_url)
        except Exception as exc:
            self.log.warning(
                "target page load timeout or failed: error_type=%s",
                type(exc).__name__,
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(6)

    def open_store(self, store_info, isWebDriverReadOnlyMode=0, isprivacy=0, isHeadless=0, cookieTypeSave=0,jsInfo=""):
        if not store_info:
            raise RuntimeError("startBrowser 失败：店铺 browserOauth/browserId 为空")
        request_id = str(uuid.uuid4())
        data = {
            "action": "startBrowser"
            , "isWaitPluginUpdate": 0
            , "isHeadless": isHeadless
            , "requestId": request_id
            , "isWebDriverReadOnlyMode": isWebDriverReadOnlyMode
            , "cookieTypeLoad": 0
            , "cookieTypeSave": cookieTypeSave
            , "runMode": "1"
            , "isLoadUserPlugin": False
            , "pluginIdType": 1
            , "privacyMode": isprivacy
            , "notPromptForDownload": 1  # 1 不弹，0：弹  下载文件的时候是否弹窗提示选择文件保存路径
        }
        data.update(self.user_info)

        if store_info.isdigit():
            data["browserId"] = store_info
        else:
            data["browserOauth"] = store_info

        if len(str(jsInfo)) > 2:
            data["injectJsInfo"] = json.dumps(jsInfo)

        self.log.info("正在请求紫鸟启动店铺，最长等待 120 秒（requestId=%s）", request_id)
        r = self.send_http(data, timeout=120)
        if not isinstance(r, dict):
            raise RuntimeError("startBrowser 在 120 秒内无有效响应，已跳过该店铺")
        if str(r.get("statusCode")) == "0":
            return r
        elif str(r.get("statusCode")) == "-10003":
            self.log.error("startBrowser login error: %s", _safe_response_summary(r))
            raise RuntimeError("operation aborted")
        else:
            error_message = str(r.get("err") or r.get("LastError") or "未知错误")
            if "browser_type" in error_message.lower():
                self.log.error(
                    "紫鸟店铺资料异常：browser_type 为空。请在紫鸟客户端打开该店铺资料，"
                    "检查平台/浏览器类型并重新保存；本次将跳过该店铺。"
                )
            self.log.error("startBrowser failed: %s", _safe_response_summary(r))
            raise RuntimeError(
                "startBrowser 失败："
                f"statusCode={r.get('statusCode')} requestId={r.get('requestId')}"
            )
    def close_store(self,browser_oauth):
        request_id = str(uuid.uuid4())
        data = {
            "action": "stopBrowser"
            , "requestId": request_id
            , "duplicate": 0
            , "browserOauth": browser_oauth
        }
        data.update(self.user_info)

        r = self.send_http(data, timeout=8)
        if r is None:
            self.log.warning("stopBrowser timeout after 8 seconds; continue cleanup")
            return None
        if str(r.get("statusCode")) == "0":
            return r
        elif str(r.get("statusCode")) == "-10003":
            self.log.error("stopBrowser login error: %s", _safe_response_summary(r))
            raise RuntimeError("operation aborted")
        else:
            self.log.warning(
                "stopBrowser returned non-zero, continue cleanup: %s",
                _safe_response_summary(r),
            )
            return r

    def quit_driver_safely(self, driver, store_name, timeout=10):
        quit_error = {}

        def _quit():
            try:
                driver.quit()
            except Exception as exc:
                quit_error["message"] = str(exc)
                quit_error["type"] = type(exc).__name__

        quit_thread = threading.Thread(target=_quit, daemon=True)
        quit_thread.start()
        quit_thread.join(timeout)
        if quit_thread.is_alive():
            self.log.warning(f"{store_name}: driver.quit 超过 {timeout} 秒未返回，已继续后续流程")
        elif quit_error:
            message = quit_error.get("message", "")
            if "not connected to DevTools" in message or "disconnected" in message:
                self.log.warning(f"{store_name}: driver.quit 时浏览器已断开 DevTools，按已关闭处理")
            else:
                self.log.warning(
                    "%s: driver.quit 执行异常：error_type=%s",
                    store_name,
                    quit_error.get("type", "unknown"),
                )

    def close_driver_window_safely(self, driver, store_name, timeout=3):
        close_error = {}

        def _close():
            try:
                driver.close()
            except Exception as exc:
                close_error["message"] = str(exc)
                close_error["type"] = type(exc).__name__

        close_thread = threading.Thread(target=_close, daemon=True)
        close_thread.start()
        close_thread.join(timeout)
        if close_thread.is_alive():
            self.log.warning(f"{store_name}: driver.close 超过 {timeout} 秒未返回，继续关闭店铺")
        elif close_error:
            message = close_error.get("message", "")
            if "not connected to DevTools" in message or "disconnected" in message:
                self.log.warning(f"{store_name}: driver.close 时浏览器已断开 DevTools，按已关闭处理")
            else:
                self.log.warning(
                    "%s: driver.close 执行异常：error_type=%s",
                    store_name,
                    close_error.get("type", "unknown"),
                )

    def _is_background_window_mode(self):
        return str(self.operator_config.get("browser_window_mode") or "normal").strip().lower() == "background"

    def _move_window_offscreen(self, driver, store_name):
        if not self._is_background_window_mode():
            return
        try:
            driver.set_window_position(-32000, -32000)
            self.log.info(f"{store_name}: 浏览器窗口已移到屏幕外后台运行")
        except Exception as exc:
            self.log.warning(
                "%s: 浏览器窗口移到屏幕外失败：error_type=%s",
                store_name,
                type(exc).__name__,
            )

    def _restore_window_onscreen(self, driver, store_name):
        if not self._is_background_window_mode():
            return
        try:
            driver.set_window_position(50, 50)
            try:
                driver.set_window_size(1280, 900)
            except Exception:
                pass
            self.log.info(f"{store_name}: 已将浏览器窗口恢复到屏幕内，方便人工查看")
        except Exception as exc:
            self.log.warning(
                "%s: 浏览器窗口恢复失败：error_type=%s",
                store_name,
                type(exc).__name__,
            )

    def use_one_browser_run_task(self, user_info, socket_port, driver_folder_path, task_list, func_dict, browser):
        """
        打开一个店铺运行脚本
        :param browser: 店铺信息
        """
        # 如果要指定店铺ID, 获取方法:登录紫鸟客户端->账号管理->选择对应的店铺账号->点击"查看账号"进入账号详情页->账号名称后面的ID即为店铺ID
        store_name = browser.get("browserName")
        max_reopen_attempts = 3

        for attempt in range(1, max_reopen_attempts + 1):
            store_id = browser.get('browserOauth')
            driver = None
            should_restore_window = False
            reopen_requested = False

            try:
                # 打开店铺
                self.log.info(f"=====打开店铺：{store_name}（第 {attempt}/{max_reopen_attempts} 次）=====")
                ret_json = self.open_store(store_id)
                self.log.info("startBrowser response: %s", _safe_response_summary(ret_json))
                store_id = ret_json.get("browserOauth")
                if store_id is None:
                    store_id = ret_json.get("browserId")
                # 使用驱动实例开启会话
                driver = self.get_driver(ret_json)
                if driver is None:
                    self.log.info(f"=====关闭店铺：{store_name}=====")
                    self.close_store(store_id)
                    return
                try:
                    driver.set_page_load_timeout(45)
                    driver.set_script_timeout(45)
                except Exception:
                    pass

                # 获取ip检测页地址
                driver._ziniao_loader = self
                driver._ziniao_browser_oauth = browser.get('browserOauth')
                driver._ziniao_current_store_id = store_id
                driver._ziniao_store_name = store_name
                driver._ziniao_attempt_index = attempt
                driver._ziniao_max_reopen_attempts = max_reopen_attempts
                ip_check_url = ret_json.get("ipDetectionPage")
                if not ip_check_url:
                    self.log.error("ip检测页地址为空，请升级紫鸟浏览器到最新版")
                    raise RuntimeError("operation aborted")

                # 设置元素查找等待时间-全局隐式等待
                driver.implicitly_wait(60)
                self._move_window_offscreen(driver, store_name)
                ip_usable = self.open_ip_check(driver, ip_check_url)
                if ip_usable:
                    self.log.info("ip检测通过，打开店铺平台主页")
                    self.open_launcher_page(driver, ret_json.get("launcherPage"))
                    # 打开店铺平台主页后进行后续自动化操作
                    self.log.info(f"=====进行脚本执行：{store_name}=====")
                    download_path = ret_json.get("downloadPath")
                    if not download_path.endswith('/') and not download_path.endswith('\\'):
                        download_path = download_path + '\\'
                    for task_name in self.task_list:
                        func = self.func_dict.get(task_name)
                        if func:
                            self.log.info(f"开始执行任务：{task_name}")
                            result = func(driver, store_name, download_path)
                            status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
                            if status == "reopen_store":
                                reopen_requested = True
                                self.log.warning(
                                    f"{store_name}: 任务要求关闭店铺后重新打开，"
                                    f"当前第 {attempt}/{max_reopen_attempts} 次"
                                )
                                break
                            if status == "failed":
                                should_restore_window = True
                            self.log.info(f"任务：{task_name}执行完毕")
                    if reopen_requested and attempt < max_reopen_attempts:
                        self.log.warning(f"{store_name}: 准备关闭当前店铺并重新打开")
                    else:
                        self.log.info(f"=====脚本全部执行完毕：{store_name}=====")
                else:
                    self.log.error("ip检测不通过，请检查")
                    should_restore_window = True
            except (Exception, SystemExit) as exc:
                should_restore_window = True
                self.log.error(
                    "店铺执行异常：store=%s error_type=%s",
                    store_name,
                    type(exc).__name__,
                )
            finally:
                if driver is not None:
                    if should_restore_window and not reopen_requested:
                        self._restore_window_onscreen(driver, store_name)
                    else:
                        self.close_driver_window_safely(driver, store_name)
                self.log.error(f"=====关闭店铺：{store_name}=====")
                try:
                    self.close_store(store_id)
                except (Exception, SystemExit) as exc:
                    self.log.error(
                        "%s: 紫鸟关闭店铺异常：error_type=%s",
                        store_name,
                        type(exc).__name__,
                    )
                if driver is not None:
                    self.quit_driver_safely(driver, store_name)

            if reopen_requested and attempt < max_reopen_attempts:
                time.sleep(2)
                continue
            return

    def use_all_browser_run_task_with_thread_pool(self,browser_list):
        """
        使用线程池控制最大并发线程数
        """
        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            # 使用 partial 固定其它参数，只循环 browser
            func = partial(self.use_one_browser_run_task, self.user_info, self.socket_port,self.driver_folder_path, self.task_list, self.func_dict)
            # func = partial(self.use_one_browser_run_task, self.user_info, self.socket_port, self.driver_folder_path, self.task_list, self.func_dict)
            list(executor.map(func, browser_list))

    def use_all_browser_run_task(self, browser_list):
        """
        循环打开所有店铺运行脚本
        :param browser_list: 店铺列表
        """
        for browser in browser_list:
            # self.use_one_browser_run_task(browser)
            self.use_one_browser_run_task(self.user_info, self.socket_port,self.driver_folder_path, self.task_list, self.func_dict,browser)

    def get_exit(self):
        """
        关闭客户端
        :return:
        """
        data = {"action": "exit", "requestId": str(uuid.uuid4())}

        data.update(self.user_info)

        self.log.info("======退出紫鸟客户端======")
        result = self.send_http(data, timeout=8)
        if result is None:
            self.log.warning("紫鸟客户端退出接口无响应，已继续收尾")
        else:
            self.log.info("exit response: %s", _safe_response_summary(result))
