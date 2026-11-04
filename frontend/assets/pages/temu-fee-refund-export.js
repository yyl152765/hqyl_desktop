(() => {
  const $ = HQYL.$;
  const outputTool = "temu_fee_refund_export";
  let running = false, preparing = false, snapshot = null, profile = "", revision = 0, polling = false, initialized = false, accountId = "";
  let outputSaveQueue = Promise.resolve();
  const activeId = () => HQYL.activeAccount("ziniao")?.id || "";
  const accountPayload = () => ({account_id: activeId()});
  const busy = () => running || preparing;
  const lines = value => value.trim().split(/\r?\n/).map(line => line.trim());
  const issue = message => { $("startIssues").textContent = message; $("startIssues").hidden = !message; };
  const validation = () => {
    const stores = lines($("storeNames").value);
    if (!$("storeNames").value.trim() || !$("startDate").value || !$("endDate").value || !$("outputDir").value.trim()) return "请填写店铺、日期和输出目录。";
    if (stores.some(name => !name)) return "店铺名中间不能有空行。";
    if (new Set(stores.map(name => name.toLowerCase())).size !== stores.length) return "店铺名不能重复，请检查输入。";
    if ($("startDate").value > $("endDate").value) return "开始日期不能晚于结束日期。";
    return "";
  };
  function controls() {
    ["storeNames", "startDate", "endDate", "outputDir", "chooseOutputBtn"].forEach(id => { $(id).disabled = busy(); });
    $("startBtn").disabled = busy() || Boolean(validation());
    $("openDirectoryBtn").disabled = !snapshot?.output_dir;
    const merged = snapshot?.consolidated;
    $("openConsolidatedBtn").disabled = !merged?.file;
    $("openConsolidatedBtn").textContent = merged?.status === "partial" ? "打开合并总表（不完整）" : "打开合并总表";
    const note = $("consolidatedMessage");
    note.hidden = !["success", "partial", "failed", "running"].includes(merged?.status);
    note.className = merged?.status === "partial" || merged?.status === "failed" ? "temu-export-warning" : "temu-export-note";
    if (merged?.status === "partial") note.textContent = "合并总表不完整，仅包含成功导出的数据。请查看下方失败项。";
    else if (merged?.status === "failed") note.textContent = `合并总表生成失败：${merged.error || "请查看运行日志"}`;
    else if (merged?.status === "success") {
      const counts = merged.row_counts || {};
      note.textContent = `合并总表已生成：发货面单费 ${counts.shipping ?? 0} 条，退货面单费 ${counts.return_label ?? 0} 条，退货退款 ${counts.refund ?? 0} 条。`;
    } else note.textContent = merged?.status === "running" ? "正在生成合并总表。" : "";
  }
  async function call(method, payload) {
    const result = await HQYL.api()[method](payload);
    if (!result.ok) throw new Error(result.error || "操作失败");
    return result;
  }
  async function openPath(path) {
    try { await call("open_path", path); } catch (error) { HQYL.showToast(error.message || String(error)); }
  }
  function saveOutputDir() {
    const output_dir = $("outputDir").value.trim();
    const operation = outputSaveQueue.catch(() => {}).then(() => call("save_scoped_output_dirs", {tool: outputTool, output_dir}));
    outputSaveQueue = operation;
    return operation;
  }
  function render(result) {
    if (!result || typeof result !== "object") return;
    snapshot = result;
    $("batchLabel").textContent = result.run_id || "本次结果";
    $("resultMessage").textContent = result.completion_message || result.message || (result.status === "running" ? "正在逐店导出。" : "任务已结束，请检查各店结果。");
    const labels = {pending: "等待", running: "运行中", success: "成功", no_data: "无数据", failed: "失败", partial: "部分完成"};
    const body = $("storeResults"); body.replaceChildren();
    (result.stores || []).forEach(store => {
      const row = document.createElement("tr");
      [store.store_name || "—", store.operator_name || "—", labels[store.status] || store.status || "—"].forEach(value => { const cell = document.createElement("td"); cell.textContent = String(value); row.appendChild(cell); });
      ["shipping", "return_label", "refund"].forEach(kind => {
        const item = store.exports?.[kind], cell = document.createElement("td");
        if (!item) cell.textContent = "—";
        else {
          const state = document.createElement("span"); state.textContent = labels[item.status] || item.status || "—"; cell.appendChild(state);
          if (item.file) { const button = document.createElement("button"); button.type = "button"; button.className = "secondary compact-btn"; button.textContent = "打开文件"; button.addEventListener("click", () => openPath(item.file)); cell.appendChild(button); }
          if (item.error) { const detail = document.createElement("small"); detail.textContent = item.error; cell.appendChild(detail); }
        }
        row.appendChild(cell);
      });
      const error = document.createElement("td"); error.textContent = store.error || store.message || "—"; row.appendChild(error); body.appendChild(row);
    });
    if (!body.childNodes.length) { const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 7; cell.textContent = "暂无逐店结果"; row.appendChild(cell); body.appendChild(row); }
    controls();
  }
  async function updateProgress(path) {
    if (!path || polling) return;
    polling = true; const version = revision, account = activeId();
    try {
      const result = await call("get_temu_fee_refund_progress", {...accountPayload(), manifest_path: path});
      if (version === revision && account === activeId()) render(result.result);
    } catch (error) { if (version === revision) $("resultMessage").textContent = `读取进度失败：${error.message}`; }
    finally { polling = false; }
  }
  async function start() {
    if (busy()) return;
    const error = validation(); if (error) { issue(error); return; }
    if (!activeId()) { HQYL.openAccountDialog("ziniao", start); return; }
    const payload = {...accountPayload(), store_names: $("storeNames").value.trim(), start_date: $("startDate").value, end_date: $("endDate").value, output_dir: $("outputDir").value.trim()};
    const version = ++revision; issue("");
    await HQYL.startTask(async () => {
      try { await saveOutputDir(); const result = await call("start_temu_fee_refund_export", payload); if (version === revision && payload.account_id === activeId()) profile = result.context?.profile || profile; return result; }
      catch (error) { if (version === revision) issue(error.message || String(error)); throw error; }
    }, () => version === revision && payload.account_id === activeId());
    controls();
  }
  async function loadInfo() {
    const version = revision, account = activeId();
    try {
      const result = await call("get_temu_fee_refund_info", accountPayload());
      if (version !== revision || account !== activeId()) return;
      profile = result.profile || "";
      if (!$("outputDir").value.trim()) $("outputDir").value = result.output_dir || "";
      if (result.latest) render(result.latest);
    } catch (error) { if (version === revision) issue(error.message || String(error)); }
    controls();
  }
  const page = {
    key: "temu_fee_refund_export", taskKey: "temu_fee_refund_export", title: "TEMU 面单费与退货退款导出",
    taskContextFilter: context => Boolean(activeId()) && context.account_id === activeId() && (!profile || !context.profile || context.profile === profile),
    async init(info) {
      accountId = activeId();
      $("exportForm").addEventListener("submit", event => { event.preventDefault(); void start(); });
      ["storeNames", "startDate", "endDate", "outputDir"].forEach(id => $(id).addEventListener("input", () => { const filled = ["storeNames", "startDate", "endDate", "outputDir"].every(field => $(field).value.trim()); issue(filled ? validation() : ""); controls(); }));
      $("outputDir").addEventListener("change", () => { void saveOutputDir().catch(error => issue(error.message || String(error))); });
      $("chooseOutputBtn").addEventListener("click", async () => { preparing = true; controls(); try { await outputSaveQueue.catch(() => {}); const result = await HQYL.api().choose_scoped_output_dir({tool: outputTool, kind: "output", current_dir: $("outputDir").value.trim()}); if (result.ok) $("outputDir").value = result.path; else if (!result.cancelled) throw new Error(result.error); } catch (error) { issue(error.message || String(error)); } finally { preparing = false; controls(); } });
      $("openDirectoryBtn").addEventListener("click", () => { if (snapshot?.output_dir) void openPath(snapshot.output_dir); });
      $("openConsolidatedBtn").addEventListener("click", () => { if (snapshot?.consolidated?.file) void openPath(snapshot.consolidated.file); });
      initialized = true; controls(); await loadInfo();
    },
    onAccountsChanged() { if (!initialized || accountId === activeId()) return; accountId = activeId(); profile = ""; revision++; snapshot = null; running = false; $("storeNames").value = ""; $("storeResults").replaceChildren(); $("resultMessage").textContent = "账号已切换，请开始新任务。"; $("batchLabel").textContent = "尚无批次"; controls(); void loadInfo(); },
    setRunning(value) { running = value; controls(); },
    resetResult() { snapshot = null; $("resultMessage").textContent = "正在启动，请稍候…"; $("storeResults").replaceChildren(); controls(); },
    onTaskUpdate(task) { if ((task.status === "running" || task.status === "pending") && task.context?.manifest_path) void updateProgress(task.context.manifest_path); },
    applyResult(result) { revision++; render(result); },
  };
  HQYL.boot(page);
})();
