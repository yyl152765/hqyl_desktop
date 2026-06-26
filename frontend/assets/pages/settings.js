(() => {
  const $ = HQYL.$;
  let updateTaskId = "";
  let updatePollTimer = 0;
  let latestUpdate = null;
  const ACCOUNT_MANAGERS = {
    mabang: {
      label: "马帮",
      select: "mabangAccountSelect",
      status: "mabangAccountStatus",
      deleteButton: "deleteMabangAccountBtn",
      detail: "mabangAccountDetail",
      name: "selectedAccountName",
      username: "selectedAccountUsername",
      card: "mabangProviderCard",
    },
    ziniao: {
      label: "紫鸟",
      select: "ziniaoAccountSelect",
      status: "ziniaoAccountStatus",
      deleteButton: "deleteZiniaoAccountBtn",
      detail: "ziniaoAccountDetail",
      name: "selectedZiniaoAccountName",
      username: "selectedZiniaoAccountUsername",
      card: "ziniaoProviderCard",
    },
    bigseller: {
      label: "BigSeller",
      select: "bigsellerAccountSelect",
      status: "bigsellerAccountStatus",
      deleteButton: "deleteBigsellerAccountBtn",
      detail: "bigsellerAccountDetail",
      name: "selectedBigsellerAccountName",
      username: "selectedBigsellerAccountUsername",
      card: "bigsellerProviderCard",
    },
  };

  function renderAccountManager(vendor) {
    const meta = ACCOUNT_MANAGERS[vendor];
    const accounts = HQYL.vendorAccounts(vendor);
    const active = HQYL.activeAccount(vendor);
    const select = $(meta.select);
    const status = $(meta.status);
    const deleteButton = $(meta.deleteButton);
    const detail = $(meta.detail);
    const name = $(meta.name);
    const username = $(meta.username);
    select.textContent = "";
    if (!accounts.length) {
      const option = document.createElement("option");
      option.textContent = `尚未绑定${meta.label}账号`;
      select.appendChild(option);
      select.disabled = true;
    } else {
      accounts.forEach((account) => {
        const option = document.createElement("option");
        const company = vendor === "ziniao" && account.extra ? account.extra.company : "";
        option.value = account.id;
        option.textContent = `${company || account.name} · ${account.username}`;
        option.selected = Boolean(active && active.id === account.id);
        select.appendChild(option);
      });
      select.disabled = false;
    }
    status.textContent = accounts.length ? `已绑定 ${accounts.length} 个` : "未绑定";
    status.classList.toggle("empty", !accounts.length);
    deleteButton.disabled = !active;
    detail.classList.toggle("empty", !active);
    const company = vendor === "ziniao" && active && active.extra ? active.extra.company : "";
    name.textContent = active ? company || active.name : "尚未绑定账号";
    username.textContent = active ? active.username : `点击“添加${meta.label}账号”开始绑定`;
    detail.querySelector(".account-avatar").textContent = active ? (company || active.name || active.username).slice(0, 1) : "?";
  }

  function renderAccounts() {
    Object.keys(ACCOUNT_MANAGERS).forEach(renderAccountManager);
  }

  async function selectAccount(vendor) {
    const select = $(ACCOUNT_MANAGERS[vendor].select);
    if (!select.value) return;
    try {
      const result = await HQYL.api().select_account(vendor, select.value);
      if (!result.ok) throw new Error(result.error || "切换账号失败");
      HQYL.applyAccountState(result.account_state || {});
      HQYL.showToast("运行账号已切换");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function deleteAccount(vendor) {
    const account = HQYL.activeAccount(vendor);
    if (!account || !window.confirm(`确认删除账号“${account.name} · ${account.username}”？`)) return;
    try {
      const result = await HQYL.api().delete_account(account.id);
      if (!result.ok) throw new Error(result.error || "删除账号失败");
      HQYL.applyAccountState(result.account_state || {});
      HQYL.showToast("账号已删除");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function saveSettings() {
    try {
      const result = await HQYL.api().save_settings({ output_dir: $("outputDir").value.trim(), rows_per_page: Number($("rowsPerPage").value || 500), update_manifest_url: $("updateManifestUrl").value.trim() });
      if (!result.ok) throw new Error(result.error || "配置保存失败");
      HQYL.state.outputDir = result.settings.output_dir || $("outputDir").value.trim();
      HQYL.showToast("配置已保存");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  function captchaSettingsPayload() {
    const payload = { captcha_username: $("captchaUsername").value.trim() };
    const password = $("captchaPassword").value;
    if (password) payload.captcha_password = password;
    return payload;
  }

  function updateCaptchaPasswordHint(configured) {
    $("captchaPassword").placeholder = configured ? "密码已保存；留空表示不修改" : "请输入图鉴密码";
  }

  async function saveCaptchaSettings() {
    const button = $("saveCaptchaSettingsBtn");
    button.disabled = true;
    try {
      const payload = captchaSettingsPayload();
      if (!payload.captcha_username) throw new Error("请输入打码平台账号");
      const result = await HQYL.api().save_settings(payload);
      if (!result.ok) throw new Error(result.error || "打码平台账号保存失败");
      $("captchaPassword").value = "";
      updateCaptchaPasswordHint(Boolean(result.settings.captcha_password_configured));
      $("captchaBalanceStatus").textContent = "账号已保存，可查询最新余额";
      $("captchaBalanceStatus").className = "captcha-balance-status success";
      HQYL.showToast("打码平台账号已保存");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    } finally {
      button.disabled = false;
    }
  }

  function captchaBalanceText(data) {
    const balance = data && (data.balance ?? data.score ?? data.money);
    if (balance !== undefined && balance !== null && String(balance) !== "") return `当前余额：${balance}`;
    return `账户信息：${JSON.stringify(data || {})}`;
  }

  async function queryCaptchaBalance() {
    const button = $("queryCaptchaBalanceBtn");
    const status = $("captchaBalanceStatus");
    if (typeof HQYL.api().query_captcha_balance !== "function") {
      status.textContent = "客户端后端版本较旧，请完全关闭并重新打开应用后再试";
      status.className = "captcha-balance-status error";
      button.disabled = true;
      return;
    }
    button.disabled = true;
    status.textContent = "正在查询余额…";
    status.className = "captcha-balance-status loading";
    try {
      const payload = {
        username: $("captchaUsername").value.trim(),
        password: $("captchaPassword").value,
      };
      const result = await HQYL.api().query_captcha_balance(payload);
      if (!result.ok) throw new Error(result.error || "余额查询失败");
      status.textContent = captchaBalanceText(result.data);
      status.className = "captcha-balance-status success";
    } catch (error) {
      status.textContent = error.message || String(error);
      status.className = "captcha-balance-status error";
    } finally {
      button.disabled = false;
    }
  }

  async function chooseOutputDir() {
    const result = await HQYL.api().choose_output_dir($("outputDir").value);
    if (result.ok) $("outputDir").value = result.path;
    else if (!result.cancelled) HQYL.showToast(result.error || "选择目录失败");
  }

  async function saveUpdateUrl() {
    await HQYL.api().save_settings({ update_manifest_url: $("updateManifestUrl").value.trim() });
  }

  function resetUpdateAction() {
    window.clearInterval(updatePollTimer);
    updatePollTimer = 0;
    updateTaskId = "";
    latestUpdate = null;
    $("updateActionPanel").hidden = true;
    $("updateProgressPanel").hidden = true;
  }

  async function checkForUpdates() {
    resetUpdateAction();
    $("updateStatus").textContent = "正在检查更新...";
    try {
      await saveUpdateUrl();
      const result = await HQYL.api().check_for_updates();
      if (!result.configured) { $("updateStatus").textContent = "未配置更新源。"; return; }
      if (!result.ok) throw new Error(result.error || "检查更新失败");
      if (!result.update_available) { $("updateStatus").textContent = `当前已是最新版本 ${result.current_version}`; return; }
      latestUpdate = result;
      $("updateStatus").textContent = `发现新版本 ${result.latest_version}。${result.notes || ""}`;
      $("availableUpdateVersion").textContent = result.latest_version;
      $("updateActionPanel").hidden = false;
    } catch (error) {
      $("updateStatus").textContent = error.message || String(error);
    }
  }

  function renderUpdateProgress(percent, text) {
    const safe = Math.max(0, Math.min(100, Number(percent) || 0));
    $("updateProgressBar").style.width = `${safe}%`;
    $("updateProgressPercent").textContent = `${safe}%`;
    $("updateProgressText").textContent = text;
  }

  function extractProgress(logs) {
    let percent = 2;
    let text = "正在下载更新包";
    (logs || []).forEach((line) => {
      const match = String(line).match(/下载进度\s+(\d+)%/);
      if (match) { percent = Number(match[1]); text = `正在下载更新包 ${percent}%`; }
      else if (String(line).includes("校验")) { percent = Math.max(percent, 99); text = "正在校验安装包"; }
    });
    return { percent, text };
  }

  async function pollUpdate() {
    if (!updateTaskId) return;
    try {
      const task = await HQYL.api().get_update_task_status(updateTaskId);
      if (!task.ok) throw new Error(task.error || "读取下载进度失败");
      const progress = extractProgress(task.logs);
      renderUpdateProgress(progress.percent, progress.text);
      if (task.status === "failed") throw new Error(task.error || "更新下载失败");
      if (task.status !== "success") return;
      window.clearInterval(updatePollTimer);
      renderUpdateProgress(100, "下载完成，正在打开安装程序");
      const result = await HQYL.api().install_downloaded_update((task.result || {}).installer_path || "");
      if (!result.ok) throw new Error(result.error || "安装程序启动失败");
      $("updateStatus").textContent = "安装程序已启动，请按向导完成更新。";
    } catch (error) {
      window.clearInterval(updatePollTimer);
      $("downloadUpdateBtn").disabled = false;
      $("updateStatus").textContent = error.message || String(error);
    }
  }

  async function startUpdateDownload() {
    if (!latestUpdate) return;
    $("downloadUpdateBtn").disabled = true;
    $("updateProgressPanel").hidden = false;
    renderUpdateProgress(0, "正在准备下载");
    try {
      const task = await HQYL.api().start_update_download();
      if (!task.ok) throw new Error(task.error || "下载启动失败");
      updateTaskId = task.id;
      await pollUpdate();
      updatePollTimer = window.setInterval(pollUpdate, 600);
    } catch (error) {
      $("downloadUpdateBtn").disabled = false;
      $("updateStatus").textContent = error.message || String(error);
    }
  }

  function hideDingTalkConfigTableEditor() {
    return $("dingtalkUserMapInfo");
  }

  function updateDingTalkCredentialHint(settings) {
    const dingtalk = (settings || {}).dingtalk || {};
    $("dingtalkAppKey").value = "";
    $("dingtalkAppSecret").value = "";
    $("dingtalkAppKey").placeholder = dingtalk.app_key_configured
      ? `已保存：${dingtalk.app_key_masked || "******"}；留空表示不修改`
      : "请输入钉钉应用 AppKey";
    $("dingtalkAppSecret").placeholder = dingtalk.app_secret_configured
      ? "AppSecret 已保存；留空表示不修改"
      : "请输入钉钉应用 AppSecret";
    const count = Number(dingtalk.user_count || 0);
    const info = hideDingTalkConfigTableEditor();
    if (info) {
      info.textContent = count > 0
        ? `已加载 ${count} 条钉钉用户配置。寄样登记时输入操作人姓名，系统会自动校验并绑定 UserID。`
        : "未加载到钉钉用户配置表，请检查内置 user_map.yaml。";
      info.className = `captcha-balance-status dingtalk-user-status ${count > 0 ? "success" : "error"}`;
    }
    $("dingtalkSettingsStatus").textContent = count > 0 ? `钉钉用户配置表已加载：${count} 条` : "未加载到钉钉用户配置表";
    $("dingtalkSettingsStatus").className = `captcha-balance-status ${count > 0 ? "success" : "error"}`;
  }

  async function saveDingTalkSettings() {
    const button = $("saveDingtalkSettingsBtn");
    button.disabled = true;
    try {
      const dingtalk = {};
      const appKey = $("dingtalkAppKey").value.trim();
      const appSecret = $("dingtalkAppSecret").value;
      if (appKey) dingtalk.app_key = appKey;
      if (appSecret) dingtalk.app_secret = appSecret;
      const result = await HQYL.api().save_settings({ dingtalk });
      if (!result.ok) throw new Error(result.error || "钉钉设置保存失败");
      updateDingTalkCredentialHint(result.settings || {});
      HQYL.showToast("钉钉设置已保存");
    } catch (error) {
      $("dingtalkSettingsStatus").textContent = error.message || String(error);
      $("dingtalkSettingsStatus").className = "captcha-balance-status error";
      HQYL.showToast(error.message || String(error));
    } finally {
      button.disabled = false;
    }
  }

  const page = {
    key: "settings",
    title: "设置",
    onAccountsChanged: renderAccounts,
    async init(info) {
      const settings = info.settings || {};
      $("outputDir").value = settings.output_dir || "";
      $("rowsPerPage").value = settings.rows_per_page || 500;
      $("updateManifestUrl").value = settings.update_manifest_url || "";
      $("captchaUsername").value = settings.captcha_username || "";
      updateCaptchaPasswordHint(Boolean(settings.captcha_password_configured));
      updateDingTalkCredentialHint(settings);
      if (typeof HQYL.api().query_captcha_balance !== "function") {
        $("queryCaptchaBalanceBtn").disabled = true;
        $("captchaBalanceStatus").textContent = "客户端后端版本较旧，请完全关闭并重新打开应用后再试";
        $("captchaBalanceStatus").className = "captcha-balance-status error";
      }
      renderAccounts();
      $("addMabangAccountBtn").addEventListener("click", () => HQYL.openAccountDialog("mabang"));
      $("addZiniaoAccountBtn").addEventListener("click", () => HQYL.openAccountDialog("ziniao"));
      $("addBigsellerAccountBtn").addEventListener("click", () => HQYL.openAccountDialog("bigseller"));
      Object.keys(ACCOUNT_MANAGERS).forEach((vendor) => {
        const meta = ACCOUNT_MANAGERS[vendor];
        $(meta.select).addEventListener("change", () => selectAccount(vendor));
        $(meta.deleteButton).addEventListener("click", () => deleteAccount(vendor));
      });
      $("saveSettingsBtn").addEventListener("click", saveSettings);
      $("saveDingtalkSettingsBtn").addEventListener("click", saveDingTalkSettings);
      $("saveCaptchaSettingsBtn").addEventListener("click", saveCaptchaSettings);
      $("queryCaptchaBalanceBtn").addEventListener("click", queryCaptchaBalance);
      $("chooseDirBtn").addEventListener("click", chooseOutputDir);
      $("checkUpdateBtn").addEventListener("click", checkForUpdates);
      $("downloadUpdateBtn").addEventListener("click", startUpdateDownload);
      const focus = new URLSearchParams(location.search).get("focus");
      if (ACCOUNT_MANAGERS[focus]) window.setTimeout(() => $(ACCOUNT_MANAGERS[focus].card).scrollIntoView({ behavior: "smooth", block: "center" }), 100);
    },
  };

  HQYL.boot(page);
})();
