(() => {
  const $ = HQYL.$;
  const RESULT_PAGE_SIZE = 50;
  const PREVIEW_LIMIT = 500;
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
  let resumeRequest = null;
  let continuingFailed = false;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function metricLabel(metric = $("metric").value) { return metric === "sales" ? "销量" : "浏览量"; }
  function updateMetricRule() { $("metricRule").textContent = `每店单商品最高${metricLabel()}`; }
  function isExportPending(result = lastResult) {
    if (!result || result.is_preview || result.is_complete !== false || result.output_file) return false;
    const confirmed = Number(result.confirmed_count ?? (Number(result.matched_count || 0) + Number(result.not_found_count || 0)));
    return Number(result.failed_count || 0) === 0 && confirmed >= Number(result.sku_count || 0);
  }

  function updateControls() {
    const busy = running || inspecting || preparing;
    ["runBtn", "sourceFile", "chooseFileBtn", "inspectFileBtn", "metric", "outputDir", "chooseDirBtn", "resumeCheckpoint", "chooseCheckpointBtn"].forEach((id) => { $(id).disabled = busy; });
    $("sheetName").disabled = busy || !$("sheetName").value;
    $("runBtn").textContent = running ? "正在对标…" : inspecting || preparing ? "正在读取文件…" : "开始对标并导出";
    $("retryFailedBtn").hidden = !resumeRequest;
    $("retryActions").hidden = !resumeRequest;
    $("retryFailedBtn").disabled = busy || !resumeRequest;
    $("retryFailedBtn").textContent = isExportPending() ? (running ? "正在重新导出…" : "重新导出已确认结果") : (running ? "正在补查…" : "继续补查失败项");
  }

  function setSheets(sheets, selected) {
    const names = Array.isArray(sheets) ? sheets.filter((name) => typeof name === "string" && name) : [];
    $("sheetName").innerHTML = names.length
      ? names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")
      : '<option value="">请先选择 Excel</option>';
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
      applyInspection(await HQYL.api().inspect_bigseller_benchmark_file(sourcePath), sourcePath);
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
      const result = await HQYL.api().choose_bigseller_benchmark_file();
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
      const result = await HQYL.api().choose_bigseller_benchmark_checkpoint();
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

  async function runBenchmark() {
    if (running || inspecting || preparing) return;
    const sourcePath = $("sourceFile").value.trim();
    if (!sourcePath) { HQYL.showToast("请选择来源 Excel"); $("sourceFile").focus(); return; }
    preparing = true;
    updateControls();
    try {
      if (inspectedPath !== sourcePath && !await inspectSource()) return;
      const sheetName = $("sheetName").value;
      if (!sheetName) { HQYL.showToast("请选择需要处理的工作表"); return; }
      const selectedOutputDir = $("outputDir").value.trim() || outputDir;
      if (!selectedOutputDir) { HQYL.showToast("请选择输出目录"); return; }
      if (!HQYL.activeAccount("bigseller")) { HQYL.openAccountDialog("bigseller", runBenchmark); return; }
      outputDir = selectedOutputDir;
      const metric = $("metric").value;
      const checkpoint = $("resumeCheckpoint").value.trim();
      const task = await HQYL.startTask(() => {
        HQYL.appendLog(`准备按${metricLabel(metric)}对标：${sourcePath} / ${sheetName}；Shopee 在售商品，SKU 模糊搜索（含子SKU）。`);
        return HQYL.api().start_bigseller_sku_benchmark({ source_file: sourcePath, sheet_name: sheetName, metric, output_dir: selectedOutputDir, ...(checkpoint ? { resume_checkpoint: checkpoint } : {}) });
      });
      if (!task) setProgress(progressCompleted, progressTotal, "任务启动失败，请查看运行日志后重试");
    } finally { preparing = false; updateControls(); }
  }

  async function retryFailed() {
    if (running || inspecting || preparing || !resumeRequest) return;
    if (!HQYL.activeAccount("bigseller")) { HQYL.openAccountDialog("bigseller", retryFailed); return; }
    const request = { ...resumeRequest };
    const previousResult = lastResult;
    const exportPending = isExportPending(previousResult);
    preparing = true;
    continuingFailed = true;
    restoreInputs(request);
    updateControls();
    try {
      const task = await HQYL.startTask(() => {
        HQYL.appendLog(`${exportPending ? "重新导出已确认结果" : "继续补查失败项"}：${request.source_file} / ${request.sheet_name} / ${metricLabel(request.metric)}；沿用已确认结果${exportPending ? "，无需重新查询" : ""}。`);
        return HQYL.api().start_bigseller_sku_benchmark(request);
      });
      if (!task && previousResult) {
        page.applyResult(previousResult);
        setProgress(progressCompleted, progressTotal, `${exportPending ? "导出" : "补查"}未启动，上次结果已保留，请查看日志并处理原因后继续`);
      }
    } finally { continuingFailed = false; preparing = false; updateControls(); }
  }

  function renderResultPage() {
    const pageCount = Math.max(1, Math.ceil(resultRows.length / RESULT_PAGE_SIZE));
    resultPage = Math.max(1, Math.min(resultPage, pageCount));
    const states = { matched: { label: "已匹配", className: "success" }, not_found: { label: "未匹配", className: "no_data" }, failed: { label: "查询失败", className: "failed" } };
    $("resultTableBody").innerHTML = resultRows.slice((resultPage - 1) * RESULT_PAGE_SIZE, resultPage * RESULT_PAGE_SIZE).map((row) => {
      const status = states[row.status] || { label: "未知状态", className: "" };
      return `<tr><td title="${escapeHtml(row.sku)}">${escapeHtml(row.sku)}</td><td>${escapeHtml(row.shop_name || "—")}</td><td>${escapeHtml(row.metric_value ?? "—")}</td><td>${escapeHtml(row.shop_count ?? "—")}</td><td>${escapeHtml(row.item_id || "—")}</td><td><span class="collection-state ${status.className}">${status.label}</span></td><td title="${escapeHtml(row.message)}">${escapeHtml(row.message || "—")}</td></tr>`;
    }).join("");
    $("resultEmpty").hidden = resultRows.length > 0;
    $("resultPrevBtn").disabled = resultPage <= 1;
    $("resultNextBtn").disabled = resultPage >= pageCount;
    $("resultPageText").textContent = `第 ${resultPage} / ${pageCount} 页`;
  }

  function restoreInputs(context) {
    const sourceChanged = context.source_file && $("sourceFile").value.trim() !== context.source_file;
    if (context.source_file) $("sourceFile").value = context.source_file;
    if (context.sheet_name) {
      const available = Array.from($("sheetName").options).some((option) => option.value === context.sheet_name);
      if (sourceChanged || !available) setSheets([context.sheet_name], context.sheet_name);
      else $("sheetName").value = context.sheet_name;
    }
    if (["views", "sales"].includes(context.metric)) $("metric").value = context.metric;
    if (context.output_dir) { outputDir = context.output_dir; $("outputDir").value = outputDir; }
    $("resumeCheckpoint").value = context.resume_checkpoint || "";
    updateMetricRule();
  }

  const page = {
    key: "bigseller_sku_benchmark",
    taskKey: "bigseller_sku_benchmark",
    title: "滞销SKU爆款对标",
    async init(info) {
      outputDir = info.settings?.output_dir || "";
      $("outputDir").value = outputDir;
      $("benchmarkForm").addEventListener("submit", (event) => { event.preventDefault(); return runBenchmark(); });
      $("sourceFile").addEventListener("input", () => { inspectedPath = ""; $("resumeCheckpoint").value = ""; setSheets([], ""); $("sourceFileHint").textContent = "路径已更新，请读取工作表列表。"; });
      $("chooseFileBtn").addEventListener("click", chooseSource);
      $("inspectFileBtn").addEventListener("click", inspectSource);
      $("metric").addEventListener("change", () => { $("resumeCheckpoint").value = ""; updateMetricRule(); });
      $("sheetName").addEventListener("change", () => { $("resumeCheckpoint").value = ""; });
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
        setProgress(progressCompleted, progressTotal, isExportPending() ? "正在恢复已确认结果并准备重新导出 Excel…" : "正在恢复已确认结果并准备补查失败项…");
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
      $("sourceRowsText").textContent = "正在读取所选工作表";
      $("matchedCount").textContent = "0";
      $("unmatchedCount").textContent = "0 / 0";
      $("outputFile").textContent = "未生成";
      setProgress(0, 0, "正在读取文件并准备对标，请稍候…");
      updateControls();
    },
    onTaskUpdate(task) {
      if (task.id && task.id !== restoredTaskId) { restoredTaskId = task.id; restoreInputs({ ...(task.result || {}), ...(task.context || {}) }); }
      let completed = progressCompleted;
      let total = Number(task.context?.sku_count || progressTotal);
      if (task.result) {
        total = Number(task.result.sku_count || total);
        completed = Number(task.result.confirmed_count ?? (Number(task.result.matched_count || 0) + Number(task.result.not_found_count || 0)));
      } else if (task.progress && Number.isFinite(Number(task.progress.total))) { total = Number(task.progress.total); completed = Number(task.progress.confirmed_count ?? task.progress.completed ?? 0); }
      else {
        for (const line of (task.logs || []).slice().reverse()) {
          const match = String(line).match(/\[BS对标进度\s+(\d+)\/(\d+)\]/);
          if (match) { completed = Number(match[1]); total = Number(match[2]); break; }
        }
      }
      if (task.status === "running" || task.status === "pending") {
        if (total) $("skuCount").textContent = String(total);
        const latestProgress = (task.logs || []).slice().reverse().find((line) => /\[BS对标进度|\[BS补查\s+第|等待/.test(String(line)));
        const message = latestProgress && /等待|\[BS补查\s+第/.test(String(latestProgress))
          ? String(latestProgress).replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, "")
          : completed ? "正在逐条确认；失败项会等待后自动补查" : "正在读取文件、登录或准备查询，请稍候…";
        setProgress(completed, total, message);
      } else if (task.status === "failed") setProgress(completed, total, task.result?.is_complete === false ? (task.result.completion_message || "结果不完整，请继续补查失败项") : "任务失败，请查看运行日志后重试");
    },
    applyResult(result) {
      lastResult = result;
      outputFile = result.output_file || "";
      resultOutputDir = result.output_dir || "";
      const allRows = Array.isArray(result.rows) ? result.rows : [];
      resultRows = allRows.slice(0, PREVIEW_LIMIT);
      resultPage = 1;
      const total = Number(result.sku_count ?? resultRows.length);
      const matched = Number(result.matched_count || 0);
      const notFound = Number(result.not_found_count || 0);
      const failed = Number(result.failed_count || 0);
      const confirmed = Number(result.confirmed_count ?? (matched + notFound));
      const incomplete = result.is_complete === false || failed > 0;
      const exportPending = isExportPending(result);
      resumeRequest = incomplete && !result.is_preview && result.checkpoint_file && result.source_file && result.sheet_name
        ? Object.freeze({ source_file: result.source_file, sheet_name: result.sheet_name, metric: result.metric, output_dir: result.output_dir, resume_checkpoint: result.checkpoint_file })
        : null;
      $("resumeCheckpoint").value = "";
      const label = result.metric_label || metricLabel(result.metric);
      $("taskBadge").className = `status-pill ${incomplete ? "failed" : "success"}`;
      $("taskBadge").textContent = incomplete ? "结果不完整" : "已完成";
      $("statusText").textContent = result.completion_message || (exportPending ? "查询结果已保存，Excel 导出未完成" : incomplete ? "存在查询失败项，结果不完整" : "所有 SKU 已确认");
      $("skuCount").textContent = String(total);
      $("sourceRowsText").textContent = `来源 ${result.sheet_name || "所选工作表"} · ${Number(result.total_rows ?? total)} 行`;
      $("matchedCount").textContent = String(matched);
      $("unmatchedCount").textContent = `${notFound} / ${failed}`;
      $("outputFile").textContent = outputFile || (result.is_preview ? "演示模式，不生成文件" : "未生成");
      $("openOutputBtn").disabled = result.is_preview || (!outputFile && !resultOutputDir);
      $("openFileBtn").disabled = result.is_preview || !outputFile;
      $("queryResult").hidden = false;
      $("previewNotice").hidden = !result.is_preview;
      $("failureNotice").hidden = !incomplete;
      $("failureNotice").textContent = exportPending
        ? `查询结果已保存，Excel 导出未完成。已确认 ${confirmed} / ${total} 个 SKU。${resumeRequest ? "处理日志中的导出问题后，点击“重新导出已确认结果”，无需重新查询。" : "请查看运行日志，处理导出问题后继续。"}`
        : `已确认 ${confirmed} / ${total} 个 SKU，仍有 ${failed} 个查询失败，结果不完整；失败不能视为店铺没有数据。${result.is_preview ? "演示模式不支持补查，请在桌面平台运行真实查询。" : resumeRequest ? `${outputFile ? "可查看部分结果 Excel，或点击" : "点击"}“继续补查失败项”，沿用原文件、工作表和指标，仅补查未确认项。如遇账号或权限异常，请先处理后再继续。` : "请查看说明与日志后重新查询。"}`;
      $("metricColumn").textContent = `最高单商品${label}`;
      const exportNotice = result.is_preview ? "演示模式未生成 Excel" : outputFile ? `${incomplete ? "部分" : "完整"}结果见 Excel` : exportPending ? "查询结果已保存，Excel 导出未完成" : "Excel 尚未生成，请查看运行日志";
      $("resultSummary").textContent = `按${label}对标 ${total} 个 SKU：已匹配 ${matched}，未匹配 ${notFound}，查询失败 ${failed}。${Number(result.resumed_count) > 0 ? `沿用已确认 ${Number(result.resumed_count)} 个；` : ""}${Number(result.recovered_count) > 0 ? `本次补查恢复 ${Number(result.recovered_count)} 个；` : ""}${Number(result.retry_rounds_used) > 0 ? `自动补查 ${Number(result.retry_rounds_used)} 轮。` : ""}下方展示各 SKU 的最高指标店铺。${exportNotice}。`;
      $("resultLimitNote").textContent = result.preview_limited || allRows.length > PREVIEW_LIMIT
        ? result.is_preview ? `演示预览前 ${resultRows.length} 条，不生成 Excel` : `预览前 ${resultRows.length} 个 SKU，${exportNotice}`
        : `共 ${resultRows.length} 条，每页 ${RESULT_PAGE_SIZE} 条`;
      setProgress(confirmed, total, incomplete ? (result.completion_message || (exportPending ? "查询结果已保存，Excel 导出未完成" : "结果不完整，请处理失败原因后继续补查")) : result.is_preview ? "界面演示已完成，未执行真实查询" : "全部 SKU 已确认，对标完成");
      renderResultPage();
      updateControls();
    },
  };

  HQYL.boot(page);
})();
