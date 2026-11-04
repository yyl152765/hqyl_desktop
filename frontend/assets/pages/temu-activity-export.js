(() => {
  const $ = HQYL.$;
  const outputTool = "temu_activity_export";
  const SITES = [["PH", "sitePH", "菲律宾"], ["MY", "siteMY", "马来西亚"], ["TH", "siteTH", "泰国"]];
  const STATUS_LABELS = {pending: "等待", running: "运行中", success: "成功", failed: "失败"};
  let running = false, preparing = false, polling = false, initialized = false, accountId = "";
  let snapshot = null, profile = "", revision = 0;
  let outputSaveQueue = Promise.resolve();
  const activeId = () => HQYL.activeAccount("ziniao")?.id || "";
  const accountPayload = () => ({account_id: activeId()});
  const busy = () => running || preparing;
  const lines = value => value.trim().split(/\r?\n/).map(line => line.trim());
  const issue = message => { $("startIssues").textContent = message; $("startIssues").hidden = !message; };
  const today = () => {
    const now = new Date(Date.now() + 8 * 3600 * 1000);
    return now.toISOString().slice(0, 10);
  };
  const selectedSites = () => SITES.filter(([, id]) => $(id).checked).map(([code]) => code);

  const validation = () => {
    const raw = $("storeNames").value;
    const stores = lines(raw);
    if (!raw.trim()) return "请填写店铺名，每行一个。";
    if (stores.some(name => !name)) return "店铺名中间不能有空行。";
    if (new Set(stores.map(name => name.toLowerCase())).size !== stores.length) return "店铺名不能重复，请检查输入。";
    if (!selectedSites().length) return "请至少勾选一个本次站点。";
    if (!$("businessDate").value) return "请选择运行日期。";
    if ($("businessDate").value > today()) return "运行日期不能晚于北京时间当天。";
    if (!$("outputDir").value.trim()) return "请选择输出目录。";
    return "";
  };

  function controls() {
    ["storeNames", "sitePH", "siteMY", "siteTH", "businessDate", "outputDir", "chooseOutputBtn"].forEach(id => { $(id).disabled = busy(); });
    $("startBtn").disabled = busy() || Boolean(validation());
    $("inspectBtn").disabled = busy() || !$("storeNames").value.trim() || !selectedSites().length;
    $("openDirectoryBtn").disabled = !snapshot?.output_dir;
    $("retryBtn").disabled = busy() || !snapshot?.manifest_path || !snapshot?.can_retry;
  }

  async function call(method, payload) {
    const result = await HQYL.api()[method](payload);
    if (!result.ok) throw new Error(result.error || "操作失败");
    return result;
  }
  async function openPath(path) {
    if (!path) return;
    try { await call("open_path", path); } catch (error) { HQYL.showToast(error.message || String(error)); }
  }
  function saveOutputDir() {
    const output_dir = $("outputDir").value.trim();
    const operation = outputSaveQueue.catch(() => {}).then(() => call("save_scoped_output_dirs", {tool: outputTool, output_dir}));
    outputSaveQueue = operation;
    return operation;
  }

  function siteText(store) {
    const sites = store.exported_sites || {};
    const entries = Object.entries(sites);
    if (!entries.length) return "—";
    return entries.map(([label, rows]) => `${label} ${rows} 行`).join(" · ");
  }

  function render(result) {
    if (!result || typeof result !== "object") return;
    snapshot = result;
    $("batchLabel").textContent = result.run_id || "本次结果";
    $("resultMessage").textContent = result.completion_message || result.message || "任务已结束，请检查各店结果。";
    const summary = $("rangeSummary");
    const successes = (result.stores || []).filter(store => store.status === "success");
    if (successes.length) {
      const siteRows = result.site_rows || {};
      const split = Object.entries(siteRows).map(([label, rows]) => `${label} ${rows} 行`).join(" / ") || "—";
      summary.hidden = false;
      summary.textContent = `导出范围：${successes.length} 个店铺 · 报价 ${result.quote_rows || 0} 行 · 库存 ${result.stock_rows || 0} 行 · 站点分布 ${split}`;
    } else {
      summary.hidden = true;
      summary.textContent = "";
    }
    const body = $("storeResults");
    body.replaceChildren();
    (result.stores || []).forEach(store => {
      const row = document.createElement("tr");
      const cells = [
        store.store_name || "—",
        STATUS_LABELS[store.status] || store.status || "—",
        store.quote_rows === null || store.quote_rows === undefined ? "—" : String(store.quote_rows),
        store.stock_rows === null || store.stock_rows === undefined ? "—" : String(store.stock_rows),
        store.topic_count === null || store.topic_count === undefined ? "—" : String(store.topic_count),
        siteText(store),
      ];
      cells.forEach(value => { const cell = document.createElement("td"); cell.textContent = value; row.appendChild(cell); });
      const note = document.createElement("td");
      const text = document.createElement("span");
      text.textContent = store.error || store.result_file || "—";
      note.appendChild(text);
      if (store.result_file) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary compact-btn";
        button.textContent = "打开文件";
        button.addEventListener("click", () => openPath(store.result_file));
        note.appendChild(button);
      }
      row.appendChild(note);
      body.appendChild(row);
    });
    if (!body.childNodes.length) {
      const row = document.createElement("tr"), cell = document.createElement("td");
      cell.colSpan = 7;
      cell.textContent = "暂无逐店结果";
      row.appendChild(cell);
      body.appendChild(row);
    }
    controls();
  }

  async function updateProgress(path) {
    if (!path || polling) return;
    polling = true;
    const version = revision, account = activeId();
    try {
      const result = await call("get_temu_activity_export_progress", {...accountPayload(), manifest_path: path});
      if (version === revision && account === activeId()) render(result.result);
    } catch (error) {
      if (version === revision) $("resultMessage").textContent = `读取进度失败：${error.message}`;
    } finally {
      polling = false;
    }
  }

  async function start() {
    if (busy()) return;
    const error = validation();
    if (error) { issue(error); return; }
    if (!activeId()) { HQYL.openAccountDialog("ziniao", start); return; }
    const payload = {
      ...accountPayload(),
      store_names: $("storeNames").value.trim(),
      sites: selectedSites(),
      business_date: $("businessDate").value,
      output_dir: $("outputDir").value.trim(),
    };
    const version = ++revision;
    issue("");
    $("inspectResult").hidden = true;
    await HQYL.startTask(async () => {
      try {
        await saveOutputDir();
        const task = await call("start_temu_activity_export", payload);
        if (version === revision && payload.account_id === activeId()) profile = task.context?.profile || profile;
        return task;
      } catch (error) {
        if (version === revision && payload.account_id === activeId()) issue(error.message || String(error));
        throw error;
      }
    }, () => version === revision && payload.account_id === activeId());
    controls();
  }

  async function retry() {
    if (busy() || !snapshot?.manifest_path) return;
    if (!activeId()) { HQYL.openAccountDialog("ziniao", retry); return; }
    const version = ++revision;
    issue("");
    await HQYL.startTask(async () => {
      try {
        return await call("retry_temu_activity_export", {manifest_path: snapshot.manifest_path});
      } catch (error) {
        if (version === revision) issue(error.message || String(error));
        throw error;
      }
    }, () => version === revision && accountPayload().account_id === activeId());
    controls();
  }

  async function inspect() {
    if (busy()) return;
    if (!activeId()) { HQYL.openAccountDialog("ziniao", inspect); return; }
    const stores = lines($("storeNames").value).filter(Boolean);
    if (!stores.length) { issue("请先填写一个店铺名再检查页面结构。"); return; }
    preparing = true;
    controls();
    const panel = $("inspectResult");
    panel.hidden = false;
    panel.textContent = "正在打开第一个店铺并读取页面标签，请稍候…";
    try {
      const result = await call("inspect_temu_activity_page", {account_id: activeId(), store_names: stores[0], sites: selectedSites()});
      const labels = Object.entries(result.labels || {}).map(([label, count]) => `${label} ×${count}`).join("、") || "未识别到任何约定标签";
      panel.textContent = [
        `店铺：${result.store_name || stores[0]}`,
        `页面：${result.title || ""} ${result.url || ""}`.trim(),
        `已识别标签：${labels}`,
        `活动类型：${(result.activity_types_found || []).join("、") || "未找到"}`,
        `商品计数：共 ${result.counters?.total ?? "—"} / 已选 ${result.counters?.selected ?? "—"}`,
        `可选复选项：${result.checkable_count ?? 0} 个`,
        `正文摘要：${(result.body_sample || "").slice(0, 400)}`,
      ].join("\n");
    } catch (error) {
      panel.textContent = `页面检查失败：${error.message || String(error)}`;
    } finally {
      preparing = false;
      controls();
    }
  }

  async function loadInfo() {
    const version = revision, account = activeId();
    try {
      const result = await call("get_temu_activity_export_info", accountPayload());
      if (version !== revision || account !== activeId()) return;
      if (!$("outputDir").value.trim()) $("outputDir").value = result.output_dir || "";
      if (!$("businessDate").value) $("businessDate").value = result.business_date || today();
      if (result.latest) render(result.latest);
    } catch (error) {
      if (version === revision) issue(error.message || String(error));
    }
    controls();
  }

  const page = {
    key: "temu_activity_export", taskKey: "temu_activity_export", title: "TEMU 活动报名导出",
    taskContextFilter: context => Boolean(activeId()) && context.account_id === activeId() && (!profile || !context.profile || context.profile === profile),
    async init() {
      accountId = activeId();
      $("exportForm").addEventListener("submit", event => { event.preventDefault(); void start(); });
      ["storeNames", "businessDate", "outputDir", "sitePH", "siteMY", "siteTH"].forEach(id => $(id).addEventListener("input", () => { issue(validation()); controls(); }));
      ["sitePH", "siteMY", "siteTH"].forEach(id => $(id).addEventListener("change", () => { issue(validation()); controls(); }));
      $("outputDir").addEventListener("change", () => { void saveOutputDir().catch(error => issue(error.message || String(error))); });
      $("chooseOutputBtn").addEventListener("click", async () => {
        preparing = true; controls();
        try {
          await outputSaveQueue.catch(() => {});
          const result = await HQYL.api().choose_scoped_output_dir({tool: outputTool, kind: "output", current_dir: $("outputDir").value.trim()});
          if (result.ok) { $("outputDir").value = result.path; issue(validation()); }
          else if (!result.cancelled) throw new Error(result.error);
        } catch (error) {
          issue(error.message || String(error));
        } finally {
          preparing = false; controls();
        }
      });
      $("inspectBtn").addEventListener("click", () => { void inspect(); });
      $("openDirectoryBtn").addEventListener("click", () => { void openPath(snapshot?.output_dir); });
      $("retryBtn").addEventListener("click", () => { void retry(); });
      initialized = true;
      controls();
      await loadInfo();
    },
    onAccountsChanged() {
      if (!initialized || accountId === activeId()) return;
      accountId = activeId(); profile = ""; revision++; snapshot = null; running = false;
      $("storeNames").value = "";
      $("storeResults").replaceChildren();
      $("inspectResult").hidden = true;
      $("rangeSummary").hidden = true;
      $("resultMessage").textContent = "账号已切换，请开始新任务。";
      $("batchLabel").textContent = "尚无批次";
      controls();
      void loadInfo();
    },
    setRunning(value) { running = value; controls(); },
    resetResult() {
      snapshot = null;
      $("resultMessage").textContent = "正在启动，请稍候…";
      $("storeResults").replaceChildren();
      $("rangeSummary").hidden = true;
      controls();
    },
    onTaskUpdate(task) {
      if ((task.status === "running" || task.status === "pending") && task.context?.manifest_path) void updateProgress(task.context.manifest_path);
    },
    applyResult(result) { revision++; render(result); },
  };
  HQYL.boot(page);
})();
