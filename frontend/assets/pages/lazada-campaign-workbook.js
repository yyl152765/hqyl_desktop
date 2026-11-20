(() => {
  const $ = HQYL.$;
  const MODE_LABELS = { auto: "自动识别", plain: "按已有活动价筛选", invited: "按推荐价填充", regular: "普通活动模板", mixed: "多种处理方式" };
  let snapshot = null, ready = false, running = false, preparing = false, preview = false, supportsSkuFilter = false, supportsRemovedSkuExport = false;
  let supportsEqualPriceRepricing = false;
  let removedSkuIndex = -1, removedSkuOrigin = null, exportingRemovedSkus = false, removedSkuVersion = 0;
  let maxFiles = 100, epoch = 0;
  const busy = () => running || preparing || exportingRemovedSkus;
  const count = value => Number(value || 0).toLocaleString("zh-CN");
  const taskFilter = version => context => epoch === version && context.workflow === "local_excel" && Array.isArray(context.source_files);
  const paths = () => {
    const seen = new Set();
    return $("sourcePaths").value.split(/\r?\n/).map(path => path.trim().replace(/^"(.*)"$/, "$1")).filter(path => {
      const key = path.toLowerCase();
      if (!path || seen.has(key)) return false;
      seen.add(key); return true;
    });
  };
  const fileName = path => String(path).split(/[\\/]/).pop();
  const skuPricePath = () => $("skuPriceFile").value.trim().replace(/^"(.*)"$/, "$1");

  async function call(method, payload) {
    const bridge = HQYL.api();
    const response = payload === undefined ? await bridge[method]() : await bridge[method](payload);
    if (response?.cancelled) return response;
    if (!response?.ok) throw new Error(response?.error || "操作失败");
    return response;
  }

  function showIssue(message = "") { $("formIssues").textContent = message; $("formIssues").hidden = !message; }
  function controls() {
    const disabled = !ready || busy(), selected = paths();
    ["chooseFilesBtn", "sourcePaths", "chooseSkuPriceBtn", "skuPriceFile", "processingMode", "discountThreshold", "campaignOutputDir", "chooseOutputBtn"].forEach(id => { $(id).disabled = disabled; });
    ["chooseSkuPriceBtn", "skuPriceFile"].forEach(id => { $(id).disabled = disabled || !supportsSkuFilter; });
    $("repriceEqualPrices").disabled = disabled || !supportsEqualPriceRepricing;
    $("clearSkuPriceBtn").disabled = disabled || !skuPricePath();
    $("clearFilesBtn").disabled = disabled || !selected.length;
    $("exportTemplateBtn").disabled = disabled || !$("campaignOutputDir").value.trim();
    $("inspectBtn").disabled = disabled || !selected.length || selected.length > maxFiles;
    $("startBtn").disabled = $("inspectBtn").disabled || !$("campaignOutputDir").value.trim();
    $("startBtn").textContent = running ? "正在处理…" : preview ? "演示筛选结果" : "导出筛选 Excel";
    $("campaignForm").setAttribute("aria-busy", String(busy()));
    document.querySelectorAll("[data-remove-file]").forEach(button => { button.disabled = disabled; });
    $("openArchiveBtn").disabled = Boolean(snapshot?.preview) || !snapshot?.archive_file;
    $("openSummaryBtn").disabled = Boolean(snapshot?.preview) || !snapshot?.output_file;
    $("openDirectoryBtn").disabled = Boolean(snapshot?.preview) || !snapshot?.output_dir;
    document.querySelectorAll("[data-view-removed-skus]").forEach(button => { button.disabled = disabled; });
    const removedFile = snapshot?.files?.[removedSkuIndex];
    $("exportRemovedSkusBtn").disabled = disabled || !supportsRemovedSkuExport || Boolean(snapshot?.preview) || !removedFile?.removed_sku_file || !HQYL.state.taskId;
    $("exportRemovedSkusBtn").textContent = exportingRemovedSkus ? "正在导出…" : "导出 Excel";
    if (!snapshot) $("fileCount").textContent = count(selected.length);
  }

  function renderFiles() {
    const list = $("sourceFileList"); list.replaceChildren();
    const selected = paths();
    selected.forEach((path, index) => {
      const item = document.createElement("li"), text = document.createElement("span"), button = document.createElement("button");
      text.textContent = fileName(path); text.title = path;
      button.type = "button"; button.className = "secondary compact-btn"; button.textContent = "移除";
      button.dataset.removeFile = String(index); button.setAttribute("aria-label", `移除 ${fileName(path)}`);
      button.addEventListener("click", () => { $("sourcePaths").value = paths().filter((_, current) => current !== index).join("\n"); filesChanged(); });
      item.append(text, button); list.appendChild(item);
    });
    if (!selected.length) { const item = document.createElement("li"); item.className = "campaign-empty"; item.textContent = "尚未选择文件"; list.appendChild(item); }
    if (selected.length > maxFiles) showIssue(`每批最多 ${maxFiles} 个文件，请移除多余文件。`);
    controls();
  }

  function filesChanged() {
    epoch++; page.taskContextFilter = taskFilter(epoch);
    HQYL.state.taskId = "";
    if (HQYL.state.pollTimer) window.clearInterval(HQYL.state.pollTimer);
    HQYL.state.pollTimer = 0;
    page.resetResult(); showIssue(); renderFiles();
    $("inspectionMessage").textContent = "可先检查表头、处理方式和数据行数。";
    $("taskBadge").className = "status-pill idle"; $("taskBadge").textContent = "待运行";
    $("statusText").textContent = "导入表格后开始处理";
    $("logBox").textContent = "";
    $("resultBadge").textContent = "尚无结果";
    $("resultMessage").textContent = "文件已更新，设置最低折扣率后开始处理。";
  }

  async function prepare(action) {
    if (!ready || busy()) return;
    const version = epoch;
    preparing = true; showIssue(); controls();
    try { await action(); }
    catch (error) { if (epoch === version) { showIssue(error.message || String(error)); HQYL.showToast(error.message || String(error)); } }
    finally { preparing = false; controls(); }
  }

  function payload() {
    const selected = paths(), percent = Number($("discountThreshold").value);
    if (!selected.length || selected.length > maxFiles) throw new Error(`请选择 1 至 ${maxFiles} 个 Excel 文件。`);
    if (selected.some(path => !/\.(xlsx|xlsm)$/i.test(path))) throw new Error("仅支持 .xlsx 或 .xlsm 文件。");
    if (!Number.isFinite(percent) || percent <= 0 || percent > 100) throw new Error("最低折扣率请输入大于 0、且不超过 100 的百分比。");
    const output = $("campaignOutputDir").value.trim();
    if (!output) throw new Error("请选择结果保存目录。");
    const reference = skuPricePath();
    if (reference && !supportsSkuFilter) throw new Error("当前后端未支持SKU筛选，请重启或升级工作台");
    if (reference && !/\.(xlsx|xlsm)$/i.test(reference)) throw new Error("SKU价格表仅支持 .xlsx 或 .xlsm 文件。");
    const repriceEqualPrices = $("repriceEqualPrices").checked;
    if (repriceEqualPrices && !supportsEqualPriceRepricing) throw new Error("请更新工作台后使用同价改价功能。");
    return { source_files: selected, sku_price_file: reference, mode: $("processingMode").value,
             threshold: String(Number((percent / 100).toFixed(4))), reprice_equal_prices: repriceEqualPrices,
             output_dir: output };
  }

  async function start() {
    if (!ready || busy()) return;
    let request;
    try { request = payload(); } catch (error) { showIssue(error.message); return; }
    showIssue(); const version = ++epoch; page.taskContextFilter = taskFilter(version);
    closeRemovedSkus(false);
    const task = await HQYL.startTask(() => call("start_lazada_campaign_workbook", request), () => epoch === version);
    if (!task && epoch === version) { $("resultBadge").textContent = "启动失败"; $("resultMessage").textContent = "请根据日志中的提示处理后重试。"; }
    controls();
  }

  async function openPath(path) {
    if (!path || snapshot?.preview) return;
    try { await call("open_path", path); } catch (error) { HQYL.showToast(error.message || String(error)); }
  }
  function outputButton(label, path) {
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary compact-btn"; button.textContent = label;
    button.addEventListener("click", () => openPath(path)); return button;
  }
  function closeRemovedSkus(restoreFocus = true) {
    removedSkuVersion++;
    $("removedSkusPanel").hidden = true;
    $("removedSkusRows").replaceChildren();
    removedSkuIndex = -1;
    if (restoreFocus && removedSkuOrigin?.isConnected) removedSkuOrigin.focus({ preventScroll: true });
    removedSkuOrigin = null;
  }
  function showRemovedSkus(index, origin) {
    if (busy()) return;
    const file = snapshot?.files?.[index]; if (!file) return;
    removedSkuVersion++; removedSkuIndex = index; removedSkuOrigin = origin;
    $("removedSkusTitle").textContent = `${file.name || fileName(file.source_file || "文件")} · SKU价格剔除（${count(file.sku_removed_rows)}）`;
    $("removedSkusDescription").textContent = snapshot.preview ? "界面演示：仅展示示例，未生成真实文件。"
      : `仅包含通过折扣筛选后，活动价低于SKU参考价格而剔除的记录；折扣剔除的记录请查看「汇总与异常明细」。共 ${count(file.sku_removed_rows)} 行，预览前 100 行；导出 Excel 包含全部SKU价格剔除明细，同一SKU的不同来源行分别保留。`;
    if (!snapshot.preview && !file.removed_sku_file) $("removedSkusDescription").textContent += "完整明细未生成，暂不能导出，请查看文件处理提示。";
    else if (!snapshot.preview && !supportsRemovedSkuExport) $("removedSkusDescription").textContent += "当前后端未支持删除SKU导出，请重启或升级工作台。";
    $("removedSkusMessage").textContent = "";
    const rows = $("removedSkusRows"); rows.replaceChildren();
    (Array.isArray(file.removed_sku_preview) ? file.removed_sku_preview.slice(0, 100) : []).forEach(item => {
      const row = document.createElement("tr");
      [item.sku, item.activity_price, item.reference_price, item.worksheet, item.source_row, item.reason].forEach(value => {
        const cell = document.createElement("td"); cell.textContent = String(value ?? ""); row.appendChild(cell);
      });
      rows.appendChild(row);
    });
    if (!rows.childElementCount) {
      const row = document.createElement("tr"), cell = document.createElement("td");
      cell.colSpan = 6; cell.textContent = "暂无预览明细，可导出 Excel 查看完整内容。"; row.appendChild(cell); rows.appendChild(row);
    }
    $("removedSkusPanel").hidden = false; controls();
    $("removedSkusPanel").scrollIntoView({ block: "nearest" }); $("closeRemovedSkusBtn").focus({ preventScroll: true });
  }
  async function exportRemovedSkus() {
    if (busy() || !supportsRemovedSkuExport || snapshot?.preview || !snapshot?.files?.[removedSkuIndex]?.removed_sku_file || !HQYL.state.taskId) return;
    const version = removedSkuVersion, request = { task_id: HQYL.state.taskId, file_index: removedSkuIndex };
    exportingRemovedSkus = true; controls();
    try {
      const response = await call("export_lazada_campaign_removed_skus", request);
      if (version === removedSkuVersion && !response.cancelled) $("removedSkusMessage").textContent = `已导出：${response.path}`;
    } catch (error) {
      if (version === removedSkuVersion) $("removedSkusMessage").textContent = error.message || String(error);
    } finally { exportingRemovedSkus = false; controls(); }
  }
  function emptyResults(text) {
    const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 9; cell.textContent = text; row.appendChild(cell); $("fileResults").replaceChildren(row);
  }
  function renderResult(result) {
    closeRemovedSkus(false);
    snapshot = result;
    $("fileCount").textContent = count(result.file_count);
    $("completedCount").textContent = result.preview ? "演示数据 · 未处理真实文件" : `完成 ${count(result.success_count)} · 失败 ${count(result.failed_count)}`;
    $("keptCount").textContent = count(result.kept_rows);
    const adjusted = result.reprice_equal_prices === true;
    $("updatedCount").textContent = `${adjusted ? "填充或调整活动价" : "填充活动价"} ${count(result.updated_rows)} 行`;
    $("updatedRowsHeading").textContent = adjusted ? "填价或改价行" : "填价行";
    $("removedCount").textContent = count(Number(result.removed_rows || 0) + Number(result.sku_removed_rows || 0) + Number(result.unparseable_rows || 0));
    $("skuRemovedCount").textContent = `SKU价格剔除 ${count(result.sku_removed_rows)} 行`;
    $("exceptionCount").textContent = `异常行 ${count(result.unparseable_rows)} · 另存明细`;
    $("totalCount").textContent = count(result.total_rows);
    $("resultBadge").textContent = result.preview ? "演示数据" : result.is_complete ? "已完成" : Number(result.success_count) ? "部分完成" : "处理失败";
    $("resultMessage").textContent = result.preview ? "仅展示示例，未读取或生成真实 Excel。"
      : `本批 ${count(result.file_count)} 个文件，完成 ${count(result.success_count)} 个，失败 ${count(result.failed_count)} 个；保留 ${count(result.kept_rows)} 行，折扣剔除 ${count(result.removed_rows)} 行，SKU价格剔除 ${count(result.sku_removed_rows)} 行，异常 ${count(result.unparseable_rows)} 行。${result.completion_message || ""}`;
    $("taskBadge").className = `status-pill ${result.is_complete ? "success" : "failed"}`;
    $("taskBadge").textContent = result.preview ? "演示结束" : result.is_complete ? "已完成" : "结果不完整";
    $("statusText").textContent = result.preview ? "界面演示 · 未生成文件" : "结果已保存，原文件保留";
    $("fileResults").replaceChildren();
    (result.files || []).forEach((file, index) => {
      const row = document.createElement("tr"), name = document.createElement("td"), mode = document.createElement("small");
      name.textContent = file.name || fileName(file.source_file || "—"); mode.textContent = MODE_LABELS[file.mode] || file.mode || "—"; name.appendChild(mode); row.appendChild(name);
      const statusCell = document.createElement("td"), status = document.createElement("span"); status.className = `campaign-row-status ${result.preview ? "preview" : file.status === "success" ? "success" : "failed"}`;
      status.textContent = result.preview ? "演示" : file.status === "success" ? "已完成" : "失败"; statusCell.appendChild(status); row.appendChild(statusCell);
      [file.total_rows, file.updated_rows, file.kept_rows, file.removed_rows, file.sku_removed_rows, file.unparseable_rows].forEach(value => { const cell = document.createElement("td"); cell.textContent = count(value); row.appendChild(cell); });
      const output = document.createElement("td"), actions = document.createElement("div"); actions.className = "campaign-row-actions";
      if (!result.preview && file.processed_file) actions.appendChild(outputButton("筛选 Excel", file.processed_file));
      if (!result.preview && file.audit_file) actions.appendChild(outputButton("异常明细", file.audit_file));
      if (Number(file.sku_removed_rows) > 0 && (file.removed_sku_file || file.removed_sku_preview?.length)) {
        const button = document.createElement("button"); button.type = "button"; button.className = "secondary compact-btn";
        button.textContent = `SKU价格剔除（${count(file.sku_removed_rows)}）`; button.dataset.viewRemovedSkus = String(index);
        button.setAttribute("aria-controls", "removedSkusPanel");
        button.addEventListener("click", () => showRemovedSkus(index, button)); actions.appendChild(button);
      }
      if (actions.childElementCount) output.appendChild(actions);
      if (file.error) { const error = document.createElement("p"); error.className = "campaign-row-error"; error.textContent = file.error; output.appendChild(error); }
      if (!output.childElementCount) output.textContent = result.preview ? "仅展示 · 未生成文件" : "—";
      row.appendChild(output); $("fileResults").appendChild(row);
    });
    if (!result.files?.length) emptyResults("没有处理结果");
    controls();
  }

  const page = {
    key: "lazada_campaign_workbook", taskKeys: ["lazada_campaign_workbook"], title: "Excel 折扣筛选",
    taskContextFilter: taskFilter(0),
    async init(info) {
      $("campaignOutputDir").value = info.settings?.output_dir || "";
      $("campaignForm").addEventListener("submit", event => { event.preventDefault(); void start(); });
      $("sourcePaths").addEventListener("input", filesChanged);
      $("skuPriceFile").addEventListener("input", filesChanged);
      $("processingMode").addEventListener("change", () => { showIssue(); $("inspectionMessage").textContent = "处理方式已更新，可重新检查表格。"; });
      $("discountThreshold").addEventListener("input", () => { $("thresholdLabel").textContent = `${$("discountThreshold").value || "—"}%`; showIssue(); });
      $("repriceEqualPrices").addEventListener("change", () => {
        showIssue(); $("inspectionMessage").textContent = "改价选项已更新，可重新检查表格。";
      });
      $("campaignOutputDir").addEventListener("input", controls);
      $("clearFilesBtn").addEventListener("click", () => { $("sourcePaths").value = ""; filesChanged(); });
      $("chooseFilesBtn").addEventListener("click", () => prepare(async () => {
        const response = await call("choose_lazada_campaign_files"); if (response.cancelled) return;
        $("sourcePaths").value = [...paths(), ...(response.paths || [])].join("\n"); filesChanged();
      }));
      $("chooseSkuPriceBtn").addEventListener("click", () => prepare(async () => {
        const response = await call("choose_lazada_campaign_sku_price_file"); if (response.cancelled) return;
        $("skuPriceFile").value = response.path || ""; filesChanged();
      }));
      $("clearSkuPriceBtn").addEventListener("click", () => { $("skuPriceFile").value = ""; filesChanged(); });
      $("chooseOutputBtn").addEventListener("click", () => prepare(async () => {
        const response = await call("choose_output_dir", $("campaignOutputDir").value.trim());
        if (!response.cancelled) $("campaignOutputDir").value = response.path || "";
      }));
      $("inspectBtn").addEventListener("click", () => prepare(async () => {
        const result = await call("inspect_lazada_campaign_files", payload());
        const reference = result.sku_reference?.enabled ? `SKU价格表：${result.preview ? "示例" : "已读取"} ${count(result.sku_reference.sku_count)} 个SKU。\n` : "";
        $("inspectionMessage").textContent = (result.preview ? "界面演示，未读取真实 Excel。\n" : "") + reference + result.files.map(file => file.ok ? `${file.name}：${MODE_LABELS[file.mode] || file.mode}，${count(file.total_rows)} 行，异常 ${count(file.unparseable_rows)} 行。` : `${file.name}：${file.error}`).join("\n");
      }));
      $("exportTemplateBtn").addEventListener("click", () => prepare(async () => {
        const result = await call("export_lazada_campaign_template", { output_dir: $("campaignOutputDir").value.trim() });
        $("templateNotice").hidden = false;
        $("templateNotice").textContent = result.preview ? "界面演示：未生成真实模板。" : `模板已保存：${result.output_file}`;
        if (!result.preview && result.output_file) await call("open_path", result.output_file);
      }));
      $("openArchiveBtn").addEventListener("click", () => openPath(snapshot?.archive_file));
      $("openSummaryBtn").addEventListener("click", () => openPath(snapshot?.output_file));
      $("openDirectoryBtn").addEventListener("click", () => openPath(snapshot?.output_dir));
      $("closeRemovedSkusBtn").addEventListener("click", () => closeRemovedSkus());
      $("exportRemovedSkusBtn").addEventListener("click", () => void exportRemovedSkus());
      document.addEventListener("keydown", event => {
        if (event.key === "Escape" && !$("removedSkusPanel").hidden) { event.preventDefault(); closeRemovedSkus(); }
      });
      try {
        const response = await call("get_lazada_campaign_workbook_info");
        if (response.workflow !== "local_excel") throw new Error("请关闭并重新启动工作台，加载本地 Excel 处理后端。");
        preview = Boolean(response.preview); $("previewNotice").hidden = !preview;
        supportsEqualPriceRepricing = response.supports_reprice_equal_prices === true;
        $("repriceEqualPricesOption").hidden = !supportsEqualPriceRepricing;
        supportsSkuFilter = response.supports_sku_price_filter === true; $("skuSupportNotice").hidden = supportsSkuFilter;
        supportsRemovedSkuExport = response.supports_removed_sku_export === true;
        maxFiles = Number(response.max_files) || 100;
        if (response.output_dir) $("campaignOutputDir").value = response.output_dir;
        $("discountThreshold").value = String(Number((Number(response.default_threshold || .95) * 100).toFixed(2)));
        $("thresholdLabel").textContent = `${$("discountThreshold").value}%`;
        ready = true;
      } catch (error) { showIssue(error.message || String(error)); }
      controls();
    },
    setRunning(value) { running = value; controls(); },
    resetResult() {
      closeRemovedSkus(false);
      snapshot = null;
      ["keptCount", "removedCount", "totalCount"].forEach(id => { $(id).textContent = "0"; });
      $("completedCount").textContent = "完成 0 · 失败 0"; $("updatedCount").textContent = "填充活动价 0 行"; $("exceptionCount").textContent = "异常行 0 · 另存明细";
      $("updatedRowsHeading").textContent = "填价行";
      $("skuRemovedCount").textContent = "SKU价格剔除 0 行";
      emptyResults("处理完成后显示每个文件的结果。"); controls();
    },
    onTaskUpdate(task) { if (task.status === "failed" && !task.result) { $("resultBadge").textContent = "处理失败"; $("resultMessage").textContent = task.error || "处理失败，请查看日志。"; } },
    onTaskRestored(task) {
      const context = task.context || {};
      if (!page.taskContextFilter(context)) return;
      $("sourcePaths").value = context.source_files.join("\n");
      $("skuPriceFile").value = typeof context.sku_price_file === "string" ? context.sku_price_file : "";
      $("repriceEqualPrices").checked = supportsEqualPriceRepricing && context.reprice_equal_prices === true;
      if (["auto", "plain", "invited"].includes(context.mode)) $("processingMode").value = context.mode;
      if (context.threshold !== undefined) { $("discountThreshold").value = String(Number((Number(context.threshold) * 100).toFixed(2))); $("thresholdLabel").textContent = `${$("discountThreshold").value}%`; }
      if (context.output_dir) $("campaignOutputDir").value = context.output_dir;
      renderFiles();
    },
    applyResult: renderResult,
  };
  HQYL.boot(page);
})();
