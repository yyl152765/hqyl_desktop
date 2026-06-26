const HQYL = (() => {
  const state = {
    page: null,
    info: null,
    accounts: [],
    activeAccountIds: {},
    taskId: "",
    pollTimer: 0,
    outputFile: "",
    outputDir: "",
    accountContinuation: null,
  };

  const $ = (id) => document.getElementById(id);
  const VENDOR_META = {
    mabang: { label: "马帮", title: "马帮 ERP", eyebrow: "MABANG ERP", logo: "M", empty: "尚未绑定马帮账号", color: "" },
    ziniao: { label: "紫鸟", title: "紫鸟账号", eyebrow: "ZINIAO BROWSER", logo: "Z", empty: "尚未绑定紫鸟账号", color: "var(--accent-purple,#7c3aed)" },
    bigseller: { label: "BigSeller", title: "BigSeller 账号", eyebrow: "BIGSELLER", logo: "B", empty: "尚未绑定 BigSeller 账号", color: "var(--accent-orange,#f97316)" },
  };

  function vendorMeta(vendor) {
    return VENDOR_META[vendor] || { label: vendor, title: "业务账号", eyebrow: "ACCOUNT", logo: "?", empty: "尚未绑定账号", color: "" };
  }

  function api() {
    if (window.pywebview && window.pywebview.api) return window.pywebview.api;
    throw new Error("pywebview bridge is not ready");
  }

  function renderSidebar(activePage) {
    const sidebar = $("appSidebar");
    if (!sidebar) return;
    sidebar.innerHTML = `
      <div class="brand">
        <div class="brand-mark"><img src="../assets/logo.png" alt="寰球云联" /></div>
        <div class="brand-copy"><div class="brand-title">寰球云联</div><div class="brand-subtitle">自动化工作台</div></div>
      </div>
      <nav class="nav-tree" aria-label="流程导航">
        <section class="nav-section">
          <div class="nav-group-title"><span class="nav-icon">M</span><span class="nav-group-copy"><strong>马帮流程</strong><small>Mabang ERP</small></span></div>
          <div class="nav-category">
            <div class="nav-category-label"><span class="nav-category-dot"></span>菲律宾 Shopee</div>
            <a class="nav-leaf" data-page="group_sales" href="group-sales.html"><span class="nav-leaf-icon">▦</span><span class="nav-leaf-copy"><strong>各组销量报表</strong><small>按小组导出销量</small></span><span class="nav-chevron">›</span></a>
          </div>
          <div class="nav-category">
            <div class="nav-category-label"><span class="nav-category-dot"></span>TikTok</div>
            <a class="nav-leaf" data-page="sample_registration" href="sample-registration.html"><span class="nav-leaf-icon">📋</span><span class="nav-leaf-copy"><strong>网红寄样登记</strong><small>样品订单查询与导出</small></span><span class="nav-chevron">›</span></a>
          </div>
          <div class="nav-category">
            <div class="nav-category-label"><span class="nav-category-dot"></span>通用工具</div>
            <a class="nav-leaf" data-page="purchase_log" href="purchase-log.html"><span class="nav-leaf-icon">⌕</span><span class="nav-leaf-copy"><strong>SKU 采购日志</strong><small>查询采购记录</small></span><span class="nav-chevron">›</span></a>
          </div>
        </section>
        <section class="nav-section">
          <div class="nav-group-title"><span class="nav-icon">Z</span><span class="nav-group-copy"><strong>紫鸟流程</strong><small>Superbrowser</small></span></div>
          <div class="nav-category">
            <div class="nav-category-label"><span class="nav-category-dot"></span>Shopee 广告</div>
            <a class="nav-leaf" data-page="shopee_ads" href="shopee-ads.html"><span class="nav-leaf-icon">＋</span><span class="nav-leaf-copy"><strong>广告充值</strong><small>多站点店铺充值</small></span><span class="nav-chevron">›</span></a>
          </div>
        </section>
        <section class="nav-section">
          <div class="nav-group-title"><span class="nav-icon">B</span><span class="nav-group-copy"><strong>BigSeller</strong><small>Listing Sync</small></span></div>
          <div class="nav-category">
            <div class="nav-category-label"><span class="nav-category-dot"></span>Shopee 商品</div>
            <a class="nav-leaf" data-page="bigseller_sync" href="bigseller-sync.html"><span class="nav-leaf-icon">↻</span><span class="nav-leaf-copy"><strong>产品/库存同步</strong><small>接口同步在售/售完</small></span><span class="nav-chevron">›</span></a>
          </div>
        </section>
        <a class="nav-settings" data-page="settings" href="settings.html"><span class="nav-leaf-icon">⚙</span><span class="nav-leaf-copy"><strong>设置</strong><small>账号、输出与更新</small></span><span class="nav-chevron">›</span></a>
      </nav>
      <div class="side-status">
        <div class="side-status-head"><div class="side-label">当前版本</div><span class="side-channel">稳定版</span></div>
        <div class="side-version-row"><span class="side-version-prefix">v</span><div id="appVersion" class="side-version">—</div></div>
        <div id="updateSummary" class="side-hint">模块按需加载</div>
      </div>`;
    sidebar.querySelectorAll("[data-page]").forEach((link) => {
      link.classList.toggle("active", link.dataset.page === activePage);
    });
  }

  function vendorAccounts(vendor) {
    return state.accounts.filter((account) => account.vendor === vendor);
  }

  function activeAccount(vendor) {
    const activeId = state.activeAccountIds[vendor] || "";
    return state.accounts.find((account) => account.vendor === vendor && account.id === activeId) || null;
  }

  function applyAccountState(accountState) {
    state.accounts = Array.isArray(accountState.accounts) ? accountState.accounts : [];
    state.activeAccountIds = accountState.active_account_ids || {};
    ["mabang", "ziniao", "bigseller"].forEach((vendor) => {
      const account = activeAccount(vendor);
      const meta = vendorMeta(vendor);
      document.querySelectorAll(`[data-active-account-name][data-vendor="${vendor}"]`).forEach((element) => {
        if (!account) {
          element.textContent = meta.empty;
          return;
        }
        const company = vendor === "ziniao" && account.extra ? account.extra.company : "";
        element.textContent = `${company || account.name} · ${account.username}`;
      });
    });
    if (state.page && typeof state.page.onAccountsChanged === "function") {
      state.page.onAccountsChanged();
    }
  }

  function showToast(message) {
    const toast = $("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.remove("hidden");
    window.setTimeout(() => toast.classList.add("hidden"), 3200);
  }

  function setBadge(kind, text) {
    const badge = $("taskBadge");
    if (!badge) return;
    badge.className = `status-pill ${kind}`;
    badge.textContent = text;
  }

  function isLogPinnedToBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight <= 24;
  }

  function copyTextFallback(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      return document.execCommand("copy");
    } catch (_error) {
      return false;
    } finally {
      textarea.remove();
    }
  }

  async function copyLogs() {
    const box = $("logBox");
    const logs = box ? box.textContent : "";
    if (!logs.trim()) {
      showToast("暂无日志可复制");
      return;
    }

    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(logs);
        copied = true;
      } catch (_error) {
        copied = false;
      }
    }
    if (!copied) copied = copyTextFallback(logs);
    showToast(copied ? "日志已复制" : "复制失败，请手动选中日志复制");
  }

  function initializeLogControls() {
    const copyButton = $("copyLogBtn");
    if (copyButton) copyButton.addEventListener("click", copyLogs);
  }

  function appendLog(message) {
    const box = $("logBox");
    if (!box) return;
    const pinnedToBottom = isLogPinnedToBottom(box);
    box.textContent += `${box.textContent ? "\n" : ""}${message}`;
    if (pinnedToBottom) box.scrollTop = box.scrollHeight;
  }

  function resetTaskView() {
    if ($("recordCount")) $("recordCount").textContent = "0";
    if ($("outputFile")) $("outputFile").textContent = "未生成";
    if ($("statusText")) $("statusText").textContent = "任务准备中";
    if ($("logBox")) $("logBox").textContent = "";
    state.outputFile = "";
    if (state.page && typeof state.page.resetResult === "function") state.page.resetResult();
  }

  function stopPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = 0;
  }

  function setRunning(running) {
    if (state.page && typeof state.page.setRunning === "function") state.page.setRunning(running);
  }

  function renderTask(task) {
    if (!task || !task.ok) return;
    renderGlobalTaskStatus(task);
    const box = $("logBox");
    if (box) {
      const pinnedToBottom = isLogPinnedToBottom(box);
      const previousScrollTop = box.scrollTop;
      box.textContent = (task.logs || []).join("\n");
      box.scrollTop = pinnedToBottom ? box.scrollHeight : previousScrollTop;
    }
    if (task.status === "running" || task.status === "pending") {
      setBadge("running", "运行中");
      if ($("statusText")) $("statusText").textContent = "任务运行中";
      setRunning(true);
      return;
    }
    setRunning(false);
    if (task.status === "success") {
      setBadge("success", "已完成");
      if ($("statusText")) $("statusText").textContent = "任务已完成";
      if (state.page && typeof state.page.applyResult === "function") state.page.applyResult(task.result || {});
    } else if (task.status === "failed") {
      setBadge("failed", "失败");
      if ($("statusText")) $("statusText").textContent = "任务失败";
      if (task.error && box && !box.textContent.includes(task.error)) appendLog(task.error);
    }
  }

  function renderGlobalTaskStatus(task) {
    const summary = $("updateSummary");
    if (!summary || !task || !task.ok) return;
    if (task.status === "running" || task.status === "pending") {
      summary.textContent = `${task.name || "后台任务"}运行中`;
    } else if (task.status === "success") {
      summary.textContent = `${task.name || "最近任务"}已完成`;
    } else if (task.status === "failed") {
      summary.textContent = `${task.name || "最近任务"}执行失败`;
    }
  }

  async function pollTask() {
    if (!state.taskId) return;
    try {
      const task = await api().get_task_status(state.taskId);
      renderTask(task);
      if (task.status === "success" || task.status === "failed") stopPolling();
    } catch (error) {
      appendLog(error.message || String(error));
      stopPolling();
      setRunning(false);
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = window.setInterval(pollTask, 1000);
    pollTask();
  }

  async function startTask(taskFactory) {
    resetTaskView();
    setRunning(true);
    setBadge("running", "启动中");
    try {
      const task = await taskFactory();
      if (!task.ok) throw new Error(task.error || "任务启动失败");
      state.taskId = task.id;
      renderTask(task);
      startPolling();
      return task;
    } catch (error) {
      setRunning(false);
      setBadge("failed", "启动失败");
      appendLog(error.message || String(error));
      showToast(error.message || String(error));
      return null;
    }
  }

  async function restoreLatestTask(tool) {
    if (!tool || typeof api().get_latest_task_status !== "function") return;
    try {
      const task = await api().get_latest_task_status(tool);
      if (!task.ok || task.empty) return;
      state.taskId = task.id;
      renderTask(task);
      if (task.status === "running" || task.status === "pending") startPolling();
    } catch (_error) {
      // Older packaged bridges may not expose task restoration yet.
    }
  }

  async function openOutput(path = "") {
    const target = path || state.outputFile || state.outputDir;
    if (!target) return;
    const result = await api().reveal_path(target);
    if (!result.ok) showToast(result.error || "打开输出目录失败");
  }

  function ensureQuickAccountModal() {
    if ($("quickAccountModal")) return;
    document.body.insertAdjacentHTML("beforeend", `
      <div id="quickAccountModal" class="modal-backdrop hidden" role="dialog" aria-modal="true">
        <form id="quickAccountForm" class="account-dialog">
          <div class="account-dialog-header">
            <div class="account-dialog-brand"><span id="quickAccountLogo" class="account-provider-logo large">M</span><div><div id="quickAccountEyebrow" class="section-eyebrow">ACCOUNT</div><h2 id="quickAccountTitle">绑定账号</h2></div></div>
            <button id="closeQuickAccountBtn" class="modal-close" type="button">×</button>
          </div>
          <div class="account-required-notice"><strong>运行前需要绑定账号</strong><span>添加成功后会自动继续当前操作。</span></div>
          <div class="account-dialog-body">
            <label id="quickCompanyField" hidden><span>紫鸟公司名</span><input id="quickCompany" /></label>
            <label><span>账号名称 <small>选填</small></span><input id="quickAccountName" maxlength="40" /></label>
            <label><span id="quickUsernameLabel">登录账号</span><input id="quickUsername" autocomplete="username" required /></label>
            <label><span id="quickPasswordLabel">登录密码</span><input id="quickPassword" type="password" autocomplete="new-password" required /></label>
          </div>
          <div class="account-dialog-actions"><button id="cancelQuickAccountBtn" class="secondary" type="button">取消</button><button id="submitQuickAccountBtn" class="primary" type="submit">确认绑定</button></div>
        </form>
      </div>`);
    $("closeQuickAccountBtn").addEventListener("click", closeQuickAccountModal);
    $("cancelQuickAccountBtn").addEventListener("click", closeQuickAccountModal);
    $("quickAccountModal").addEventListener("click", (event) => {
      if (event.target === $("quickAccountModal")) closeQuickAccountModal();
    });
    $("quickAccountForm").addEventListener("submit", submitQuickAccount);
  }

  let quickAccountVendor = "mabang";

  function openAccountDialog(vendor, continuation = null) {
    ensureQuickAccountModal();
    quickAccountVendor = vendor;
    state.accountContinuation = continuation;
    const ziniao = vendor === "ziniao";
    const meta = vendorMeta(vendor);
    $("quickAccountForm").reset();
    $("quickCompanyField").hidden = !ziniao;
    $("quickCompany").required = ziniao;
    $("quickAccountLogo").textContent = meta.logo;
    $("quickAccountLogo").style.background = meta.color || "";
    $("quickAccountEyebrow").textContent = meta.eyebrow;
    $("quickAccountTitle").textContent = `绑定${meta.label}账号`;
    $("quickUsernameLabel").textContent = `${meta.label}账号`;
    $("quickPasswordLabel").textContent = `${meta.label}密码`;
    $("quickAccountModal").classList.remove("hidden");
    window.setTimeout(() => (ziniao ? $("quickCompany") : $("quickUsername")).focus(), 0);
  }

  function closeQuickAccountModal() {
    if ($("quickAccountModal")) $("quickAccountModal").classList.add("hidden");
    state.accountContinuation = null;
  }

  async function submitQuickAccount(event) {
    event.preventDefault();
    const button = $("submitQuickAccountBtn");
    button.disabled = true;
    button.textContent = "正在绑定...";
    try {
      const isZiniao = quickAccountVendor === "ziniao";
      const company = isZiniao ? $("quickCompany").value.trim() : "";
      const result = await api().add_account({
        vendor: quickAccountVendor,
        name: $("quickAccountName").value.trim() || company,
        username: $("quickUsername").value.trim(),
        password: $("quickPassword").value,
        extra: isZiniao ? { company } : {},
      });
      if (!result.ok) throw new Error(result.error || "账号绑定失败");
      const continuation = state.accountContinuation;
      applyAccountState(result.account_state || {});
      closeQuickAccountModal();
      showToast("账号绑定成功");
      if (typeof continuation === "function") await continuation();
    } catch (error) {
      showToast(error.message || String(error));
    } finally {
      button.disabled = false;
      button.textContent = "确认绑定";
    }
  }

  async function initialize(page) {
    state.page = page;
    renderSidebar(page.key);
    try {
      const info = await api().get_app_info();
      if (!info.ok) throw new Error(info.error || "应用信息加载失败");
      state.info = info;
      state.outputDir = (info.settings || {}).output_dir || "";
      document.title = `${page.title} - ${info.app.name}`;
      if ($("appVersion")) $("appVersion").textContent = info.app.version;
      applyAccountState(info.account_state || {});
      if (typeof page.init === "function") await page.init(info);
      if (typeof api().get_latest_task_status === "function") {
        const latestGlobalTask = await api().get_latest_task_status();
        if (latestGlobalTask.ok) renderGlobalTaskStatus(latestGlobalTask);
      }
      if (page.taskKey) await restoreLatestTask(page.taskKey);
    } catch (error) {
      showToast(error.message || String(error));
    }
  }

  function boot(page) {
    let started = false;
    const start = () => {
      if (started || !window.pywebview || !window.pywebview.api) return;
      started = true;
      initialize(page);
    };
    const domReady = () => {
      renderSidebar(page.key);
      initializeLogControls();
      const preview = location.hostname === "127.0.0.1" && new URLSearchParams(location.search).has("preview");
      if (preview && (!window.pywebview || !window.pywebview.api)) window.pywebview = { api: createPreviewApi() };
      start();
      if (!started) {
        window.addEventListener("pywebviewready", start, { once: true });
        const timer = window.setInterval(() => {
          start();
          if (started) window.clearInterval(timer);
        }, 100);
      }
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", domReady, { once: true });
    else domReady();
  }

  function createPreviewApi() {
    let accounts = [];
    let activeAccountIds = {};
    const tasks = {};
    const accountState = () => ({ accounts, active_account_ids: activeAccountIds });
    const startPreviewTask = (tool, result = {}) => {
      const task = { ok: true, id: `preview-${tool}`, tool, status: "success", logs: ["预览任务已完成"], result };
      tasks[tool] = task;
      return task;
    };
    return {
      async get_app_info() { return { ok: true, app: { name: "寰球云联自动化平台", version: "0.3.0" }, date_range: { start_date: "2026-06-01", end_date: "2026-06-23" }, settings: { output_dir: "C:\\Users\\Demo\\Desktop", rows_per_page: 500, update_manifest_url: "", captcha_username: "preview-user", captcha_password_configured: true, dingtalk: { app_key_configured: true, app_key_masked: "din****key", app_secret_configured: true, user_count: 1, operator_names: ["王小妹"] }, sales_group_ids: ["preview-group"] }, account_state: accountState(), group_sales_report: { groups: [{ id: "preview-group", name: "示例小组" }] } }; },
      async get_latest_task_status(tool) { return tasks[tool] || { ok: false, empty: true }; },
      async get_task_status(id) { return Object.values(tasks).find((task) => task.id === id) || { ok: false, error: "任务不存在" }; },
      async start_group_sales_report() { return startPreviewTask("group_sales", { group_count: 1, record_count: 16, output_file: "C:\\Preview\\销量.xlsx", output_dir: "C:\\Preview" }); },
      async start_purchase_log_query() { return startPreviewTask("purchase_log", { sku_count: 2, record_count: 8, output_file: "C:\\Preview\\采购.xlsx", output_dir: "C:\\Preview" }); },
      async start_shopee_ads_recharge() { return startPreviewTask("shopee_ads", { matched_store_count: 2, processed_store_count: 2, output_dir: "C:\\Preview" }); },
      async start_bigseller_sync() { return startPreviewTask("bigseller_sync", { processed_count: 300, success_count: 300, failed_count: 0, round_count: 1, output_file: "C:\\Preview\\BigSeller同步.xlsx", output_dir: "C:\\Preview" }); },
      async add_account(payload) { const account = { id: `preview-${Date.now()}`, vendor: payload.vendor, name: payload.name || payload.username, username: payload.username, extra: payload.extra || {} }; accounts = [...accounts, account]; activeAccountIds = { ...activeAccountIds, [payload.vendor]: account.id }; return { ok: true, account_state: accountState() }; },
      async select_account(vendor, id) { activeAccountIds = { ...activeAccountIds, [vendor]: id }; return { ok: true, account_state: accountState() }; },
      async delete_account(id) { const deleted = accounts.find((item) => item.id === id); accounts = accounts.filter((item) => item.id !== id); if (deleted) delete activeAccountIds[deleted.vendor]; return { ok: true, account_state: accountState() }; },
      async save_settings(payload) { return { ok: true, settings: payload }; },
      async query_captcha_balance() { return { ok: true, data: { balance: "88.50" } }; },
      async choose_output_dir() { return { ok: true, path: "C:\\Users\\Demo\\Desktop" }; },
      async reveal_path() { return { ok: true }; },
      async get_shopee_ads_info() { return { ok: true, sites: [{ code: "id", name: "印尼", label: "印尼" }, { code: "th", name: "泰国", label: "泰国" }, { code: "ph", name: "菲律宾", label: "菲律宾" }, { code: "vn", name: "越南", label: "越南" }, { code: "my", name: "马来", label: "马来" }], client_path: "C:\\Program Files\\Ziniao\\ziniao.exe", webdriver_path: "C:\\ziniaodriver" }; },
      async save_shopee_ads_config() { return { ok: true }; },
      async choose_client_path() { return { ok: true, path: "C:\\Program Files\\Ziniao\\ziniao.exe" }; },
      async choose_driver_path() { return { ok: true, path: "C:\\ziniaodriver" }; },
      async check_for_updates() { return { ok: true, configured: false }; },
      async get_sample_registration_config() { return { ok: true, config: { version: 1, target_doc_id: "", enable_target_update: false, dingtalk_operator_name: "", output_dir: "", groups: [] } }; },
      async save_sample_registration_config(payload) { return { ok: true, config: payload }; },
      async start_sample_store_match() { return startPreviewTask("sample_registration_match", { resolved_count: 5, unresolved_count: 1, resolved_rows: [], unresolved_rows: [] }); },
      async start_sample_registration() { return startPreviewTask("sample_registration", { processed_count: 120, online_append_count: 100, unmatched_store_count: 0, resolved_count: 10, group_count: 3, output_directory: "C:\\Preview\\寄样", output_files: [] }); },
    };
  }

  return {
    $,
    api,
    state,
    boot,
    activeAccount,
    vendorAccounts,
    applyAccountState,
    showToast,
    appendLog,
    startTask,
    openOutput,
    openAccountDialog,
  };
})();
