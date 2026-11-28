(() => {
  const $ = HQYL.$;
  const RESULT_PAGE_SIZE = 50;
  const PREVIEW_LIMIT = 500;
  const STATES = {
    matched: { label: "已匹配", className: "success" },
    not_found: { label: "ERP留存范围未找到", className: "no_data" },
    incomplete: { label: "数据待核验", className: "failed" },
    failed: { label: "查询失败", className: "failed" },
    not_run: { label: "未执行", className: "failed" },
  };
  let outputDir = "";
  let resultOutputDir = "";
  let outputFile = "";
  let resultRows = [];
  let resultPage = 1;
  let running = false;
  let inspecting = false;
  let preparing = false;
  let inspectedPath = "";
  let restoredTaskId = "";
  let progressCompleted = 0;
  let progressTotal = 0;
  let lastResult = null;
  let lastRequest = null;
  let resumeRequest = null;
  let continuingFailed = false;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function updateControls() {
    const busy = running || inspecting || preparing;
    ["runBtn", "skuText", "sourceFile", "chooseFileBtn", "inspectFileBtn", "countries", "shopIds", "outputDir", "chooseDirBtn", "resumeCheckpoint", "chooseCheckpointBtn"].forEach((id) => { $(id).disabled = busy; });
    $("sheetName").disabled = busy || !$("sheetName").value;
    $("runBtn").textContent = running ? "正在查询…" : inspecting || preparing ? "正在读取文件…" : "查询并导出";
    $("retryFailedBtn").hidden = !resumeRequest;
    $("retryActions").hidden = !resumeRequest;
    $("retryFailedBtn").disabled = busy || !resumeRequest;
    $("retryFailedBtn").textContent = running ? "正在补查…" : "继续补查失败/未执行项";
  }

  function setSheets(sheets, selected) {
    const names = Array.isArray(sheets) ? sheets.filter((name) => typeof name === "string" && name) : [];
    $("sheetName").innerHTML = names.length
      ? names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")
      : '<option value="">请选择工作表</option>';
    if (names.includes(selected)) $("sheetName").value = selected;
    updateControls();
  }

  function applyInspection(result, sourcePath) {
    if (!result.ok) throw new Error(result.error || "读取工作表失败");
    if (!Array.isArray(result.sheets) || !result.sheets.length) throw new Error("Excel 中没有可读取的工作表");
    const previousSheet = $("sheetName").value;
    setSheets(result.sheets, result.sheets.includes(previousSheet) ? previousSheet : result.sheet_name);
    inspectedPath = sourcePath;
    const warnings = Array.isArray(result.warnings) ? result.warnings.filter(Boolean).map(String) : [];
    $("sourceFileHint").textContent = `已读取 ${result.sheets.length} 个工作表，请确认需要处理的工作表。${warnings.length ? ` ${warnings.join("；")}` : ""}`;
  }

  async function inspectSource() {
    if (running || inspecting) return false;
    const sourcePath = $("sourceFile").value.trim();
    if (!sourcePath) { HQYL.showToast("请先选择或输入来源 Excel 路径"); $("sourceFile").focus(); return false; }
    inspecting = true;
    updateControls();
    $("sourceFileHint").textContent = "正在读取工作表…";
    try {
      applyInspection(await HQYL.api().inspect_erp_sku_file(sourcePath), sourcePath);
      return true;
    } catch (error) {
      inspectedPath = "";
      setSheets([], "");
      $("sourceFileHint").textContent = error.message || String(error);
      HQYL.showToast(error.message || String(error));
      return false;
    } finally { inspecting = false; updateControls(); }
  }

  async function chooseSource() {
    if (running || inspecting || preparing) return;
    inspecting = true;
    updateControls();
    try {
      const result = await HQYL.api().choose_erp_sku_file();
      if (result.cancelled) return;
      if (!result.ok) throw new Error(result.error || "选择 Excel 失败");
      $("sourceFile").value = result.path || "";
      $("resumeCheckpoint").value = "";
      inspectedPath = "";
      setSheets([], "");
      applyInspection(result, $("sourceFile").value.trim());
    } catch (error) { HQYL.showToast(error.message || String(error)); }
    finally { inspecting = false; updateControls(); }
  }

  async function chooseOutputDir() {
    try {
      const result = await HQYL.api().choose_output_dir($("outputDir").value.trim() || outputDir);
      if (result.ok) { outputDir = result.path; $("outputDir").value = result.path; }
      else if (!result.cancelled) HQYL.showToast(result.error || "选择目录失败");
    } catch (error) { HQYL.showToast(error.message || String(error)); }
  }

  async function chooseCheckpoint() {
    if (running || inspecting || preparing) return;
    preparing = true;
    updateControls();
    try {
      const result = await HQYL.api().choose_erp_sku_checkpoint();
      if (result.cancelled) return;
      if (!result.ok) throw new Error(result.error || "选择进度文件失败");
      $("resumeCheckpoint").value = result.path || "";
    } catch (error) { HQYL.showToast(error.message || String(error)); }
    finally { preparing = false; updateControls(); }
  }

  function setProgress(completed, total, message) {
    progressTotal = Math.max(0, Number(total) || 0);
    progressCompleted = Math.min(progressTotal, Math.max(0, Number(completed) || 0));
    const percent = progressTotal ? Math.min(progressCompleted < progressTotal ? 99 : 100, Math.round(progressCompleted / progressTotal * 100)) : 0;
    $("progressCount").textContent = `${progressCompleted} / ${progressTotal}`;
    $("queryProgressBar").style.width = `${percent}%`;
    $("queryProgress").setAttribute("aria-valuenow", String(percent));
    $("queryProgress").setAttribute("aria-valuetext", `已确认 ${progressCompleted} / ${progressTotal} 个 SKU`);
    $("progressText").textContent = message;
  }

  function buildRequest() {
    const request = { output_dir: $("outputDir").value.trim() || outputDir };
    const skuText = $("skuText").value.trim();
    const sourcePath = $("sourceFile").value.trim();
    if (skuText) request.sku_text = skuText;
    if (sourcePath) { request.source_file = sourcePath; request.sheet_name = $("sheetName").value; }
    if ($("countries").value.trim()) request.countries = $("countries").value.trim();
    if ($("shopIds").value.trim()) request.shop_ids = $("shopIds").value.trim();
    const checkpoint = $("resumeCheckpoint").value.trim();
    if (checkpoint) request.resume_checkpoint = checkpoint;
    return request;
  }

  async function runQuery() {
    if (running || inspecting || preparing) return;
    const skuText = $("skuText").value.trim();
    const sourcePath = $("sourceFile").value.trim();
    if (skuText && sourcePath) { HQYL.showToast("单个 SKU 输入与 Excel 文件只能二选一"); return; }
    if (!skuText && !sourcePath) { HQYL.showToast("请输入 SKU 或选择来源 Excel"); $("skuText").focus(); return; }
    preparing = true;
    updateControls();
    try {
      if (sourcePath) {
        if (inspectedPath !== sourcePath && !await inspectSource()) return;
        if (!$("sheetName").value) { HQYL.showToast("请选择需要处理的工作表"); return; }
      }
      const request = buildRequest();
      if (!request.output_dir) { HQYL.showToast("请选择输出目录"); return; }
      outputDir = request.output_dir;
      lastRequest = { ...request };
      delete lastRequest.resume_checkpoint;
      const task = await HQYL.startTask(() => {
        HQYL.appendLog(`准备查询：${sourcePath ? `${sourcePath} / ${request.sheet_name}` : "手动输入 SKU"}；Shopee 全部状态（含归档记录），SKU 精确匹配，选择商品平台创建时间最早的店铺。`);
        return HQYL.api().start_erp_sku_earliest_store(request);
      });
      if (!task) setProgress(progressCompleted, progressTotal, "任务启动失败，请查看运行日志后重试");
    } finally { preparing = false; updateControls(); }
  }

  async function retryFailed() {
    if (running || inspecting || preparing || !resumeRequest) return;
    const request = { ...resumeRequest };
    const previousResult = lastResult;
    preparing = true;
    continuingFailed = true;
    updateControls();
    try {
      const task = await HQYL.startTask(() => {
        HQYL.appendLog("继续补查失败/未执行项：沿用较早批次已确认的结果，仅补查未确认项。");
        return HQYL.api().start_erp_sku_earliest_store(request);
      });
      if (!task && previousResult) {
        page.applyResult(previousResult);
        setProgress(progressCompleted, progressTotal, "补查未启动，上次结果已保留，请查看日志并处理原因后继续");
      }
    } finally { continuingFailed = false; preparing = false; updateControls(); }
  }

  function renderResultPage() {
    const pageCount = Math.max(1, Math.ceil(resultRows.length / RESULT_PAGE_SIZE));
    resultPage = Math.max(1, Math.min(resultPage, pageCount));
    $("resultTableBody").innerHTML = resultRows.slice((resultPage - 1) * RESULT_PAGE_SIZE, resultPage * RESULT_PAGE_SIZE).map((row) => {
      const status = STATES[row.status] || { label: "未知状态", className: "" };
      const winner = row.status === "matched";
      const cell = (value) => `<td title="${escapeHtml(value)}">${escapeHtml(value || "—")}</td>`;
      return `<tr>${cell(row.sku)}${cell(winner ? row.candidate_shop_name : "")}${cell(winner ? row.candidate_shop_id : "")}${cell(winner ? row.region : "")}${cell(winner ? row.item_id : "")}${cell(winner ? row.create_time : "")}${cell(winner ? row.item_status : "")}<td><span class="collection-state ${status.className}">${status.label}</span></td>${cell(row.message)}</tr>`;
    }).join("");
    $("resultEmpty").hidden = resultRows.length > 0;
    $("resultPrevBtn").disabled = resultPage <= 1;
    $("resultNextBtn").disabled = resultPage >= pageCount;
    $("resultPageText").textContent = `第 ${resultPage} / ${pageCount} 页`;
  }

  function restoreInputs(context) {
    if (context.source_file) {
      const changed = $("sourceFile").value.trim() !== context.source_file;
      $("sourceFile").value = context.source_file;
      if (context.sheet_name) {
        const available = Array.from($("sheetName").options).some((option) => option.value === context.sheet_name);
        if (changed || !available) setSheets([context.sheet_name], context.sheet_name);
        else $("sheetName").value = context.sheet_name;
      }
    }
    if (Array.isArray(context.countries)) $("countries").value = context.countries.join(",");
    if (Array.isArray(context.shop_ids)) $("shopIds").value = context.shop_ids.join(",");
    if (context.output_dir) { outputDir = context.output_dir; $("outputDir").value = outputDir; }
    $("resumeCheckpoint").value = context.resume_checkpoint || "";
  }

  const page = {
    key: "erp_sku_earliest_store",
    taskKey: "erp_sku_earliest_store",
    title: "ERP SKU 创建时间第一店铺",
    async init(info) {
      outputDir = info.settings?.output_dir || "";
      $("outputDir").value = outputDir;
      $("erpForm").addEventListener("submit", (event) => { event.preventDefault(); return runQuery(); });
      $("sourceFile").addEventListener("input", () => { inspectedPath = ""; $("resumeCheckpoint").value = ""; setSheets([], ""); $("sourceFileHint").textContent = "路径已更新，请读取工作表列表。"; });
      $("chooseFileBtn").addEventListener("click", chooseSource);
      $("inspectFileBtn").addEventListener("click", inspectSource);
      ["sheetName", "countries", "shopIds", "skuText"].forEach((id) => $(id).addEventListener("change", () => { $("resumeCheckpoint").value = ""; }));
      $("chooseDirBtn").addEventListener("click", chooseOutputDir);
      $("chooseCheckpointBtn").addEventListener("click", chooseCheckpoint);
      $("retryFailedBtn").addEventListener("click", retryFailed);
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || resultOutputDir));
      $("openFileBtn").addEventListener("click", async () => {
        if (!outputFile) return;
        try { const result = await HQYL.api().open_path(outputFile); if (!result.ok) HQYL.showToast(result.error || "打开 Excel 失败"); }
        catch (error) { HQYL.showToast(error.message || String(error)); }
      });
      $("resultPrevBtn").addEventListener("click", () => { resultPage -= 1; renderResultPage(); });
      $("resultNextBtn").addEventListener("click", () => { resultPage += 1; renderResultPage(); });
      updateControls();
    },
    setRunning(value) { running = value; updateControls(); },
    resetResult() {
      if (continuingFailed && lastResult) {
        setProgress(progressCompleted, progressTotal, "正在恢复已确认结果并准备补查未确认项…");
        return;
      }
      lastResult = null;
      resumeRequest = null;
      outputFile = "";
      resultOutputDir = "";
      resultRows = [];
      resultPage = 1;
      $("queryResult").hidden = true;
      $("previewNotice").hidden = true;
      $("failureNotice").hidden = true;
      $("openOutputBtn").disabled = true;
      $("openFileBtn").disabled = true;
      $("skuCount").textContent = "0";
      $("sourceRowsText").textContent = "正在读取输入";
      $("matchedCount").textContent = "0";
      $("unmatchedCount").textContent = "0 / 0 / 0";
      $("outputFile").textContent = "未生成";
      setProgress(0, 0, "正在准备查询，请稍候…");
      updateControls();
    },
    onTaskUpdate(task) {
      if (task.id && task.id !== restoredTaskId) { restoredTaskId = task.id; restoreInputs({ ...(task.result || {}), ...(task.context || {}) }); }
      let completed = progressCompleted;
      let total = Number(task.context?.sku_count || progressTotal);
      if (task.result) {
        total = Number(task.result.sku_count || total);
        completed = Number(task.result.confirmed_count ?? 0);
      } else {
        for (const line of (task.logs || []).slice().reverse()) {
          const match = String(line).match(/\[ERP进度\s+(\d+)\/(\d+)\]/);
          if (match) { completed = Number(match[1]); total = Number(match[2]); break; }
        }
      }
      if (task.status === "running" || task.status === "pending") {
        if (total) $("skuCount").textContent = String(total);
        setProgress(completed, total, completed ? "正在分批查询 ERP 并保存已确认结果" : "正在读取输入并连接 ERP 只读库，请稍候…");
      } else if (task.status === "failed") setProgress(completed, total, task.result?.is_complete === false ? (task.result.completion_message || "结果不完整") : "任务失败，请查看运行日志后重试");
    },
    applyResult(result) {
      lastResult = result;
      outputFile = result.output_file || "";
      resultOutputDir = result.run_dir || result.output_dir || "";
      const allRows = Array.isArray(result.rows) ? result.rows : [];
      resultRows = allRows.slice(0, PREVIEW_LIMIT);
      resultPage = 1;
      const total = Number(result.sku_count ?? resultRows.length);
      const matched = Number(result.matched_count || 0);
      const notFound = Number(result.not_found_count || 0);
      const incompleteData = Number(result.incomplete_count || 0);
      const failed = Number(result.failed_count || 0) + Number(result.not_run_count || 0);
      const confirmed = Number(result.confirmed_count ?? (matched + notFound + incompleteData));
      const incomplete = result.is_complete === false || failed > 0 || incompleteData > 0;
      const canResume = !result.is_preview && failed > 0 && result.checkpoint_file && lastRequest;
      resumeRequest = canResume ? Object.freeze({ ...lastRequest, resume_checkpoint: result.checkpoint_file, ...(result.source_file ? { source_file: result.source_file, sheet_name: result.sheet_name } : {}) }) : null;
      $("resumeCheckpoint").value = "";
      $("taskBadge").className = `status-pill ${incomplete ? "failed" : "success"}`;
      $("taskBadge").textContent = incomplete ? "结果不完整" : "已完成";
      $("statusText").textContent = result.completion_message || (incomplete ? "存在未确认项，结果不完整" : "所有 SKU 已确认");
      $("skuCount").textContent = String(total);
      $("sourceRowsText").textContent = result.source_file ? `来源 ${result.sheet_name || "所选工作表"} · ${Number(result.total_rows ?? total)} 行` : "手动输入 SKU";
      $("matchedCount").textContent = String(matched);
      $("unmatchedCount").textContent = `${notFound} / ${incompleteData} / ${failed}`;
      $("outputFile").textContent = outputFile || (result.is_preview ? "演示模式，不生成文件" : "未生成");
      $("openOutputBtn").disabled = result.is_preview || (!outputFile && !resultOutputDir);
      $("openFileBtn").disabled = result.is_preview || !outputFile;
      $("queryResult").hidden = false;
      $("previewNotice").hidden = !result.is_preview;
      $("failureNotice").hidden = !incomplete;
      $("failureNotice").textContent = `已确认 ${confirmed} / ${total} 个 SKU：未找到 ${notFound}，数据待核验 ${incompleteData}，失败/未执行 ${failed}。失败、超时与未执行不等于“未找到”；数据待核验的 SKU 未回填第一店铺。${resumeRequest ? "点击“继续补查失败/未执行项”可沿用已确认结果。" : ""}`;
      $("resultSummary").textContent = `查询 ${total} 个 SKU：已匹配 ${matched}，ERP留存范围未找到 ${notFound}，数据待核验 ${incompleteData}，失败/未执行 ${failed}。${Number(result.resumed_count) > 0 ? `沿用较早批次已确认 ${Number(result.resumed_count)} 个。` : ""}${outputFile ? "完整结果见 Excel。" : result.is_preview ? "演示模式未生成 Excel。" : "Excel 尚未生成，请查看运行日志。"}`;
      $("resultLimitNote").textContent = result.preview_limited || allRows.length > PREVIEW_LIMIT ? `预览前 ${resultRows.length} 个 SKU，完整内容见 Excel` : `共 ${resultRows.length} 条，每页 ${RESULT_PAGE_SIZE} 条`;
      setProgress(confirmed, total, incomplete ? (result.completion_message || "结果不完整") : result.is_preview ? "界面演示已完成，未连接 ERP" : "全部 SKU 已确认，查询完成");
      renderResultPage();
      updateControls();
    },
  };

  HQYL.boot(page);
})();
