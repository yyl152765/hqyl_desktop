(() => {
  const $ = HQYL.$;
  let profile = "", snapshot = null;
  let running = false, preparing = false, polling = false, revision = 0;
  let accountId = "", initialized = false;
  const busy = () => running || preparing;
  const activeId = () => HQYL.activeAccount("ziniao")?.id || "";
  const accountPayload = () => ({account_id: activeId(), client_path: $("clientPath").value, webdriver_path: $("driverPath").value});

  function ensureAccount(continuation) {
    if (activeId()) return true;
    HQYL.openAccountDialog("ziniao", continuation);
    return false;
  }

  async function call(method, payload) {
    const result = await HQYL.api()[method](payload);
    if (!result.ok) throw new Error(result.error || "操作失败");
    return result;
  }

  function controls() {
    ["storeNames", "exportOutputDir", "chooseOutputBtn", "chooseClientBtn", "chooseDriverBtn"].forEach(id => { $(id).disabled = busy(); });
    $("startBtn").disabled = busy() || !$("storeNames").value.trim() || !$("exportOutputDir").value.trim();
    $("retryBtn").disabled = busy() || !activeId() || !snapshot?.can_retry || snapshot.profile !== profile;
    $("retryBtn").textContent = snapshot && !snapshot.failed_count && snapshot.can_retry ? "继续未完成批次 / 重试汇总" : "重试失败店铺";
    $("openSummaryBtn").disabled = !snapshot?.output_file;
    $("openDirectoryBtn").disabled = !snapshot?.output_dir;
  }

  function showIssue(message) {
    $("startIssues").hidden = !message;
    $("startIssues").textContent = message || "";
  }

  function invalidate() {
    revision++;
    showIssue("");
    controls();
  }

  async function prepare(action) {
    if (busy()) return;
    preparing = true; controls();
    try { await action(); } catch (error) {
      showIssue(error.message || String(error));
      HQYL.showToast(error.message || String(error));
    } finally { preparing = false; controls(); }
  }

  function render(result) {
    snapshot = result;
    $("storeCount").textContent = String(result.store_count || 0);
    $("completedCount").textContent = String(result.completed_count || 0);
    $("noDataCount").textContent = `无数据 ${result.no_data_count || 0} · 失败 ${result.failed_count || 0}`;
    $("recordCount").textContent = Number(result.row_count || 0).toLocaleString();
    $("outputFile").textContent = result.output_file ? (result.is_complete ? "汇总已生成" : "部分结果") : "未生成";
    $("batchLabel").textContent = result.run_id || "尚无批次";
    $("resultMessage").textContent = result.status === "running" ? "正在导出，下面显示每个店铺的最新状态。" : result.status === "interrupted" ? "上次运行已中断，可继续未完成批次。已成功的原始文件会先校验再复用。" : result.completion_message;
    if (!running && result.status !== "running") {
      $("taskBadge").className = `status-pill ${result.is_complete ? "success" : "failed"}`;
      $("taskBadge").textContent = result.is_complete ? "已完成" : "结果不完整";
      $("statusText").textContent = result.is_complete ? "汇总已保存并校验" : "可处理失败项后重试";
    }
    const labels = {pending: "等待", running: "运行中", success: "成功", no_data: "无数据", failed: "失败"};
    const body = $("storeResults"); body.replaceChildren();
    (result.stores || []).forEach(store => {
      const row = document.createElement("tr");
      const changes = [store.error, store.added_columns?.length ? `新增字段：${store.added_columns.join("、")}` : "", store.missing_columns?.length ? `缺少字段：${store.missing_columns.join("、")}` : ""].filter(Boolean).join("；");
      [store.store_name, labels[store.status] || store.status, store.raw_row_count ?? "—", store.row_count ?? "—", store.raw_file ? "查看原始文件" : "—", changes || "—"].forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 4 && store.raw_file) {
          const button = document.createElement("button");
          button.className = "secondary compact-btn"; button.type = "button"; button.textContent = value;
          button.addEventListener("click", () => openPath(store.raw_file)); cell.appendChild(button);
        } else cell.textContent = String(value);
        if (index === 0) { const small = document.createElement("small"); small.textContent = `ID ${store.store_id}`; cell.appendChild(small); }
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
    controls();
  }

  async function updateProgress(path) {
    if (polling || !path) return;
    polling = true;
    const version = revision;
    try {
      const request = {...accountPayload(), manifest_path: path};
      const result = await call("get_temu_on_sale_export_progress", request);
      if (version === revision && request.account_id === activeId() && result.result.profile === profile) render(result.result);
    } catch (error) { if (version === revision) $("resultMessage").textContent = `读取进度失败：${error.message}`; }
    finally { polling = false; }
  }

  async function start(retry = false) {
    if (busy()) return;
    if (!ensureAccount(() => start(retry))) return;
    const payload = {...accountPayload(), ...(retry ? {manifest_path: snapshot?.manifest_path} : {
      store_names: $("storeNames").value, output_dir: $("exportOutputDir").value.trim(),
    })};
    const version = ++revision;
    showIssue("");
    $("connectionNotice").textContent = retry ? "正在恢复本账号的批次…" : "正在登录紫鸟并查找填写的店铺，请稍候…";
    await HQYL.startTask(async () => {
      try {
        const task = await call(retry ? "retry_temu_on_sale_export" : "start_temu_on_sale_export", payload);
        if (version === revision && payload.account_id === activeId()) {
          profile = task.context?.profile || profile;
          $("connectionNotice").textContent = "任务已启动，按名单顺序逐店导出。";
        }
        return task;
      } catch (error) {
        if (version === revision && payload.account_id === activeId()) {
          showIssue(error.message || String(error));
          $("connectionNotice").textContent = "启动未完成，请按提示处理后重新开始。";
        }
        throw error;
      }
    });
    controls();
  }

  async function openPath(path) {
    if (!path) return;
    try { await call("open_path", path); } catch (error) { HQYL.showToast(error.message); }
  }

  async function loadInfo() {
    const request = accountPayload();
    const version = revision;
    try {
      const result = await call("get_temu_on_sale_export_info", request);
      if (version !== revision || request.account_id !== activeId()) return;
      profile = result.profile || "";
      if (!$("clientPath").value) $("clientPath").value = result.client_path || "";
      if (!$("driverPath").value) $("driverPath").value = result.webdriver_path || "";
      if (result.latest) render(result.latest);
    } catch (error) { if (version === revision) $("connectionNotice").textContent = error.message; }
    controls();
  }

  const page = {
    key: "temu_on_sale_export", taskKey: "temu_on_sale_export", title: "TEMU 在售商品导出",
    taskContextFilter: context => Boolean(activeId()) && context.account_id === activeId() && (!profile || context.profile === profile),
    async init(info) {
      $("exportOutputDir").value = info.settings?.output_dir || "";
      accountId = activeId();
      $("exportForm").addEventListener("submit", event => { event.preventDefault(); start(); });
      $("storeNames").addEventListener("input", invalidate);
      $("exportOutputDir").addEventListener("input", controls);
      $("chooseOutputBtn").addEventListener("click", () => prepare(async () => { const result = await HQYL.api().choose_output_dir($("exportOutputDir").value); if (result.ok) $("exportOutputDir").value = result.path; else if (!result.cancelled) throw new Error(result.error); }));
      [["chooseClientBtn", "clientPath", "choose_client_path"], ["chooseDriverBtn", "driverPath", "choose_driver_path"]].forEach(([button, input, method]) => {
        $(button).addEventListener("click", () => prepare(async () => {
          const result = await HQYL.api()[method]();
          if (result.ok) { $(input).value = result.path; invalidate(); }
          else if (!result.cancelled) throw new Error(result.error);
        }));
      });
      $("openSummaryBtn").addEventListener("click", () => openPath(snapshot?.output_file));
      $("openDirectoryBtn").addEventListener("click", () => openPath(snapshot?.output_dir));
      $("retryBtn").addEventListener("click", () => start(true));
      initialized = true;
      await loadInfo();
      controls();
    },
    onAccountsChanged() {
      if (!initialized || accountId === activeId()) return;
      accountId = activeId(); profile = ""; running = false;
      invalidate(); page.resetResult();
      $("logBox").textContent = "";
      $("taskBadge").className = "status-pill";
      $("taskBadge").textContent = "待运行";
      $("statusText").textContent = "请填写本账号的店铺名";
      $("connectionNotice").textContent = "账号已切换，开始导出时将使用当前账号查找店铺。";
      $("resultMessage").textContent = "所选账号尚未加载批次。";
      void loadInfo();
    },
    setRunning(value) { running = value; controls(); },
    resetResult() {
      snapshot = null;
      $("storeResults").replaceChildren();
      $("storeCount").textContent = "0";
      $("completedCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("noDataCount").textContent = "无数据 0 · 失败 0";
      $("outputFile").textContent = "未生成";
      $("batchLabel").textContent = "准备新任务";
      $("resultMessage").textContent = "正在启动，请稍候…";
      controls();
    },
    onTaskUpdate(task) { if (task.context?.account_id === activeId() && task.context?.manifest_path) void updateProgress(task.context.manifest_path); },
    applyResult(result) { if (result.manifest_path && result.profile === profile) { revision++; render(result); } },
  };
  HQYL.boot(page);
})();
