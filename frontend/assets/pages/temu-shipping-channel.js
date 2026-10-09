(() => {
  const $ = HQYL.$;
  const PAGE_SIZE = 50;
  let accountId = "";
  let revision = 0;
  let initialized = false;
  let running = false;
  let preparing = false;
  let inspecting = false;
  let inspectionRequest = 0;
  let restoring = false;
  let pendingMode = "";
  let snapshot = null;
  let workbook = null;
  let rows = [];
  let resultPage = 1;
  let taskId = "";
  let recoveryTaskId = "";
  let refreshingProgress = false;
  let progressRequest = 0;

  const sourcePath = () => $("inputFile").value.trim();
  const busy = () => running || preparing || inspecting || restoring;
  const isCurrent = (version, owner) => revision === version && accountId === owner;
  const canApply = () => Boolean(snapshot?.run_id && snapshot.selection_policy === "enabled_only" && snapshot.can_apply === true
    && (snapshot.mode === "preview" || !snapshot.complete));

  function updateControls() {
    const locked = busy();
    $("inputFile").disabled = locked;
    $("chooseFileBtn").disabled = locked;
    $("inspectFileBtn").disabled = locked || !sourcePath();
    $("previewBtn").disabled = locked || !sourcePath();
    $("applyBtn").disabled = locked || !canApply();
    $("applyBtn").textContent = snapshot?.mode === "batch" && !snapshot.complete ? "继续未完成渠道" : "开始批量设置";
    $("chooseFileBtn").textContent = inspecting ? "正在读取…" : "选择 Excel";
  }

  function setProgress(completed, total, message) {
    const count = Math.max(0, Number(total) || 0);
    const success = Math.min(count, Math.max(0, Number(completed) || 0));
    const percent = count ? Math.min(success < count ? 99 : 100, Math.round(success / count * 100)) : 0;
    $("progressCount").textContent = `${success} / ${count}`;
    $("queryProgressBar").style.width = `${percent}%`;
    $("queryProgress").setAttribute("aria-valuenow", String(percent));
    $("queryProgress").setAttribute("aria-valuetext", `已核验 ${success} / ${count} 个已开启渠道`);
    $("progressText").textContent = message;
  }

  function setNotice(message, warning = false) {
    $("resultNotice").hidden = !message;
    $("resultNotice").textContent = message;
    $("resultNotice").className = `temu-notice${warning ? " warning" : ""}`;
  }

  function renderRows() {
    const body = $("resultTableBody");
    body.replaceChildren();
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    resultPage = Math.max(1, Math.min(resultPage, pages));
    if (!rows.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.className = "temu-empty";
      cell.colSpan = 9;
      cell.textContent = snapshot ? "没有查询到已开启且可设置的 TEMU 渠道" : "请先导入每日随机长宽高 Excel";
      row.appendChild(cell);
      body.appendChild(row);
    }
    const labels = {
      ready: ["ready", "待设置"], pending: ["ready", "待设置"], success: ["success", "已修改并核验"], unchanged: ["unchanged", "无需修改"],
      failed: ["failed", "失败"], error: ["failed", "失败"], running: ["running", "正在设置"],
      saving: ["running", "正在保存"], verifying: ["running", "正在核验"], missing_data: ["failed", "数据不足"],
      missing: ["failed", "数据不足"], blocked: ["failed", "无法设置"],
    };
    const start = (resultPage - 1) * PAGE_SIZE;
    rows.slice(start, start + PAGE_SIZE).forEach((item, index) => {
      const row = document.createElement("tr");
      const addCell = (value) => {
        const cell = document.createElement("td");
        cell.textContent = String(value ?? "—");
        row.appendChild(cell);
        return cell;
      };
      addCell(start + index + 1);
      const channel = addCell(snapshot ? (item.channel_name || "—") : "等待查询渠道");
      if (item.channel_id) {
        const id = document.createElement("small");
        id.className = "temu-channel-id";
        id.textContent = `ID ${item.channel_id}`;
        channel.appendChild(id);
      }
      addCell(item.excel_row);
      ["length", "width", "height", "weight"].forEach((key) => {
        const original = item.original_values?.[key];
        const actual = item.observed_values?.[key];
        const target = item[key];
        const cell = addCell(snapshot && original != null ? `${original} → ${target ?? "—"}` : target);
        cell.className = "temu-value-change";
        if (!snapshot) return;
        const detail = document.createElement("small");
        detail.className = "temu-value-detail";
        if (actual != null) {
          const matches = Number(actual) === Number(target);
          detail.textContent = `${item.observation_stage === "after_save" ? "保存后" : "当前"}：${actual}`;
          if (!matches) {
            detail.className += " mismatch";
            const mismatch = document.createElement("span");
            mismatch.className = "temu-mismatch-label";
            mismatch.textContent = "未达目标";
            detail.appendChild(mismatch);
          }
        } else if (item.status === "ready" || item.status === "missing_data") {
          detail.textContent = target == null ? "缺少目标值" : "目标值 · 执行时读取原值";
        } else {
          detail.textContent = "未记录实际值，无法展示核验明细";
        }
        cell.appendChild(detail);
      });
      const state = addCell("");
      const badge = document.createElement("span");
      const [kind, label] = snapshot ? (labels[item.status] || ["", item.status || "待设置"]) : ["", "已读取"];
      badge.className = `temu-state ${kind}`;
      badge.textContent = label;
      state.appendChild(badge);
      addCell(item.message || "—");
      body.appendChild(row);
    });
    $("resultPageText").textContent = `第 ${resultPage} / ${pages} 页`;
    $("rowCountText").textContent = `共 ${rows.length} 行，每页 ${PAGE_SIZE} 行`;
    $("resultPrevBtn").disabled = resultPage <= 1;
    $("resultNextBtn").disabled = resultPage >= pages;
  }

  function invalidatePreview(clearWorkbook = false, invalidateRequests = true) {
    if (invalidateRequests) revision += 1;
    snapshot = null;
    taskId = "";
    recoveryTaskId = "";
    progressRequest += 1;
    refreshingProgress = false;
    if (clearWorkbook) workbook = null;
    rows = Array.isArray(workbook?.rows) ? workbook.rows : [];
    resultPage = 1;
    $("channelCount").textContent = "0";
    $("successCount").textContent = "0";
    $("changeCounts").textContent = "已修改 0 · 无需修改 0";
    $("remainingCount").textContent = "0 / 0";
    $("excelRowCount").textContent = String(workbook?.row_count || 0);
    $("sheetSummary").textContent = workbook?.sheet_name || "长、宽、高、重量";
    $("resultSummary").textContent = "请查询已开启渠道并预览，按 Excel 顺序建立对应关系；已关闭渠道不占用 Excel 行。";
    $("statusText").textContent = "请导入文件并查询渠道";
    $("taskBadge").className = "status-pill idle";
    $("taskBadge").textContent = "待预览";
    if (clearWorkbook) {
      $("fileWarning").hidden = true;
      $("fileHint").textContent = "文件已变化，请重新读取并查询预览。";
    }
    setProgress(0, 0, "等待查询渠道");
    setNotice("");
    renderRows();
    updateControls();
  }

  function applyWorkbook(response) {
    if (!response?.ok) throw new Error(response?.error || "Excel 读取失败");
    const source = response.workbook;
    if (!source || !Array.isArray(source.rows) || !source.rows.length) throw new Error("Excel 中没有有效的长宽高和重量数据");
    invalidatePreview(true);
    workbook = source;
    $("inputFile").value = response.file_path || source.input_file || sourcePath();
    rows = source.rows;
    $("excelRowCount").textContent = String(source.row_count ?? rows.length);
    $("sheetSummary").textContent = source.sheet_name || "已读取工作表";
    $("fileHint").textContent = `已读取 ${source.sheet_name || "工作表"}，共 ${source.row_count ?? rows.length} 组数据；查询预览后固定本次对应关系。`;
    const warnings = Array.isArray(source.warnings) ? source.warnings.filter(Boolean).map(String) : [];
    $("fileWarning").hidden = !warnings.length;
    $("fileWarning").textContent = warnings.join("；");
    $("statusText").textContent = "Excel 已就绪，请查询渠道并预览";
    renderRows();
    updateControls();
  }

  async function inspectFile(choose = false) {
    if (busy()) return;
    const input = sourcePath();
    if (!choose && !input) return;
    const version = revision;
    const owner = accountId;
    const request = ++inspectionRequest;
    inspecting = true;
    updateControls();
    try {
      const response = choose ? await HQYL.api().choose_temu_shipping_file()
        : await HQYL.api().inspect_temu_shipping_file({ input_file: input });
      if (!isCurrent(version, owner) || response?.cancelled) return;
      applyWorkbook(response);
    } catch (error) {
      if (isCurrent(version, owner)) { invalidatePreview(true); $("fileHint").textContent = error.message || String(error); HQYL.showToast(error.message || String(error)); }
    } finally {
      if (inspectionRequest === request) { inspecting = false; updateControls(); }
    }
  }

  async function restoreSavedProgress(version = revision, owner = accountId) {
    if (!owner) return;
    try {
      const response = await HQYL.api().get_temu_shipping_checkpoint({ account_id: owner });
      if (!isCurrent(version, owner)) return;
      if (!response?.ok) throw new Error(response?.error || "上次进度读取失败");
      if (response.result) {
        if (!applySnapshot(response.result)) return;
        if (response.result.can_apply && !response.result.complete && response.result.mode === "batch") setNotice("已恢复上次进度，继续未完成渠道会沿用原 Excel 数据和对应关系。", true);
      }
    } catch (error) {
      if (isCurrent(version, owner)) HQYL.showToast(error.message || String(error));
    }
  }

  async function refreshRunningProgress(task) {
    if (refreshingProgress || task.tool !== "temu_shipping_batch" || !task.context?.run_id) return;
    const version = revision;
    const owner = accountId;
    refreshingProgress = true;
    const request = ++progressRequest;
    try {
      const response = await HQYL.api().get_temu_shipping_checkpoint({ account_id: owner });
      if (!isCurrent(version, owner) || !running || taskId !== task.id) return;
      if (response?.ok && response.result?.run_id === task.context.run_id) {
        if (!applySnapshot(response.result)) return;
        $("taskBadge").className = "status-pill running";
        $("taskBadge").textContent = "运行中";
        $("statusText").textContent = "正在逐渠道保存并核验";
      }
    } catch (_error) { /* The task log remains available if progress cannot be read. */ }
    finally { if (request === progressRequest) refreshingProgress = false; }
  }

  async function runPreview() {
    if (busy()) return;
    const input = sourcePath();
    if (!input) { HQYL.showToast("请选择每日随机长宽高 Excel"); return; }
    const account = HQYL.activeAccount("mabang");
    if (!account) { HQYL.openAccountDialog("mabang", runPreview); return; }
    invalidatePreview(false);
    const version = revision;
    const owner = account.id;
    preparing = true;
    pendingMode = "preview";
    updateControls();
    try {
      await HQYL.startTask(() => {
        HQYL.appendLog(`查询已开启（ON）的 TEMU 渠道，并按 Excel 原始顺序对应；已关闭（OFF）渠道不占用 Excel 行：${input}`);
        return HQYL.api().start_temu_shipping_preview({ account_id: owner, input_file: input });
      });
    } finally {
      if (isCurrent(version, owner)) { preparing = false; pendingMode = ""; updateControls(); }
    }
  }

  async function runBatch() {
    if (busy() || !canApply()) return;
    const account = HQYL.activeAccount("mabang");
    if (!account || account.id !== accountId) { HQYL.showToast("账号已变化，请重新查询渠道并预览"); return; }
    const request = { account_id: account.id, run_id: snapshot.run_id };
    const version = revision;
    preparing = true;
    pendingMode = "batch";
    updateControls();
    try {
      const task = await HQYL.startTask(() => {
        HQYL.appendLog("按预览对应关系逐条设置已开启渠道的长宽高（cm）及申报重量（g），保存后核验。");
        return HQYL.api().start_temu_shipping_batch(request);
      });
      if (!task && isCurrent(version, account.id)) setNotice("设置任务未启动，本次数据已保留。请处理日志中的问题后重试。", true);
    } finally {
      if (isCurrent(version, account.id)) { preparing = false; pendingMode = ""; updateControls(); }
    }
  }

  function applySnapshot(result) {
    if (!result?.run_id || !["preview", "batch"].includes(result.mode)) return false;
    if (result.selection_policy !== "enabled_only") {
      // Clear old mappings without invalidating the request that is restoring them.
      invalidatePreview(false, false);
      const message = "渠道筛选规则已更新为仅处理已开启（ON）渠道，请重新查询预览。";
      $("statusText").textContent = message;
      setProgress(0, 0, message);
      setNotice(message, true);
      return false;
    }
    snapshot = result;
    rows = Array.isArray(result.rows) ? result.rows : [];
    if (result.input_file) $("inputFile").value = result.input_file;
    const total = Number(result.channel_count || 0);
    const success = Number(result.success_count || 0);
    const changed = Number(result.changed_count ?? rows.filter((row) => row.status === "success").length);
    const unchanged = Number(result.unchanged_count ?? rows.filter((row) => row.status === "unchanged").length);
    const failed = Number(result.failed_count || 0);
    const remaining = Math.max(0, total - success - failed);
    $("excelRowCount").textContent = String(result.excel_row_count || 0);
    $("sheetSummary").textContent = result.sheet_name || "本次 Excel";
    $("channelCount").textContent = String(total);
    $("successCount").textContent = String(success);
    $("changeCounts").textContent = `已修改 ${changed} · 无需修改 ${unchanged}`;
    $("remainingCount").textContent = `${remaining} / ${failed}`;
    $("fileHint").textContent = `${result.sheet_name || "工作表"} · ${result.excel_row_count || 0} 组数据；本次数据和对应关系已保存。`;
    const warnings = Array.isArray(result.warnings) ? result.warnings.filter(Boolean).map(String) : [];
    $("fileWarning").hidden = !warnings.length;
    $("fileWarning").textContent = warnings.join("；");
    const preview = result.mode === "preview";
    const message = result.message || (preview ? (result.can_apply ? "预览完成，可以开始批量设置" : "无法开始设置，请检查数据和已开启渠道数量")
        : result.complete ? "本次已开启渠道均已核验" : "存在未完成渠道，可处理原因后继续");
    $("resultSummary").textContent = preview
      ? `共 ${total} 个已开启渠道，已匹配 ${result.ready_count || 0} 组数据；已关闭渠道不占用 Excel 行。`
      : `本次 ${total} 个已开启渠道，已修改 ${changed} 个，无需修改 ${unchanged} 个，待处理 ${remaining} 个，失败 ${failed} 个。`;
    $("statusText").textContent = message;
    $("taskBadge").className = `status-pill ${preview ? (result.can_apply ? "idle" : "failed") : result.complete ? "success" : "failed"}`;
    $("taskBadge").textContent = preview ? (result.can_apply ? "待设置" : "预览需处理") : result.complete ? "已完成" : "部分未完成";
    setNotice(message, preview ? !result.can_apply : !result.complete || unchanged === total);
    setProgress(success, total, message);
    renderRows();
    updateControls();
    return true;
  }

  const page = {
    key: "temu_shipping_channel",
    taskKeys: ["temu_shipping_preview", "temu_shipping_batch"],
    title: "TEMU发货渠道更改",
    async init() {
      accountId = HQYL.activeAccount("mabang")?.id || "";
      initialized = true;
      $("shippingForm").addEventListener("submit", (event) => { event.preventDefault(); return runPreview(); });
      $("inputFile").addEventListener("input", () => { invalidatePreview(true); });
      $("chooseFileBtn").addEventListener("click", () => inspectFile(true));
      $("inspectFileBtn").addEventListener("click", () => inspectFile(false));
      $("applyBtn").addEventListener("click", runBatch);
      $("resultPrevBtn").addEventListener("click", () => { resultPage -= 1; renderRows(); });
      $("resultNextBtn").addEventListener("click", () => { resultPage += 1; renderRows(); });
      restoring = true;
      updateControls();
      const version = revision;
      const owner = accountId;
      try { await restoreSavedProgress(version, owner); } finally { if (isCurrent(version, owner)) { restoring = false; updateControls(); } }
    },
    async onAccountsChanged() {
      if (!initialized) return;
      const nextAccountId = HQYL.activeAccount("mabang")?.id || "";
      if (nextAccountId === accountId) return;
      accountId = nextAccountId;
      running = false;
      preparing = false;
      inspecting = false;
      inspectionRequest += 1;
      refreshingProgress = false;
      pendingMode = "";
      invalidatePreview(true);
      $("logBox").textContent = "";
      const version = revision;
      const owner = accountId;
      restoring = true;
      updateControls();
      try {
        await restoreSavedProgress(version, owner);
        if (isCurrent(version, owner) && typeof HQYL.restoreLatestTaskFromKeys === "function") {
          await HQYL.restoreLatestTaskFromKeys(page.taskKeys, (context) => isCurrent(version, owner) && page.taskContextFilter(context));
        }
      } finally { if (isCurrent(version, owner)) { restoring = false; updateControls(); } }
    },
    taskContextFilter(context) { return Boolean(accountId && context?.account_id === accountId); },
    setRunning(value) { running = value; updateControls(); },
    resetResult() {
      if (pendingMode === "preview") setProgress(0, 0, "正在读取 Excel 并查询已开启的 TEMU 渠道…");
      else if (pendingMode === "batch") setProgress(snapshot?.success_count, snapshot?.channel_count, "正在恢复本次对应关系并准备设置…");
    },
    onTaskUpdate(task) {
      if (!page.taskContextFilter(task.context)) return;
      if (task.id && task.id !== taskId) {
        taskId = task.id;
        if (task.context?.input_file) $("inputFile").value = task.context.input_file;
      }
      if (task.result && !applySnapshot(task.result)) return;
      if (task.status === "pending" || task.status === "running") {
        const total = task.progress?.total ?? snapshot?.channel_count ?? 0;
        const success = task.progress?.success_count ?? snapshot?.success_count ?? 0;
        setProgress(success, total, task.progress?.message || (task.tool === "temu_shipping_preview" ? "正在查询渠道并建立对应关系…" : "正在逐渠道保存并核验…"));
        void refreshRunningProgress(task);
      } else if (task.status === "failed" && !task.result && task.id !== recoveryTaskId) {
        recoveryTaskId = task.id;
        void restoreSavedProgress();
      }
    },
    applyResult(result) {
      // Common task rendering filters account context before delivering results.
      applySnapshot(result);
    },
  };
  HQYL.boot(page);
})();
