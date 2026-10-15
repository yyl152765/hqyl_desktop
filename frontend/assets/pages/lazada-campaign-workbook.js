(() => {
  const $ = HQYL.$;
  const MODE_LABELS = { auto: "自动识别", plain: "按已有活动价筛选", invited: "按推荐价填充", regular: "普通活动模板", mixed: "多种处理方式" };
  let snapshot = null, ready = false, running = false, preparing = false, preview = false;
  let maxFiles = 100, epoch = 0;
  const busy = () => running || preparing;
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
    ["chooseFilesBtn", "sourcePaths", "processingMode", "discountThreshold", "campaignOutputDir", "chooseOutputBtn"].forEach(id => { $(id).disabled = disabled; });
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
    return { source_files: selected, mode: $("processingMode").value, threshold: String(Number((percent / 100).toFixed(4))), output_dir: output };
  }

  async function start() {
    if (!ready || busy()) return;
    let request;
    try { request = payload(); } catch (error) { showIssue(error.message); return; }
    showIssue(); const version = ++epoch; page.taskContextFilter = taskFilter(version);
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
  function emptyResults(text) {
    const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 8; cell.textContent = text; row.appendChild(cell); $("fileResults").replaceChildren(row);
  }
  function renderResult(result) {
    snapshot = result;
    $("fileCount").textContent = count(result.file_count);
    $("completedCount").textContent = result.preview ? "演示数据 · 未处理真实文件" : `完成 ${count(result.success_count)} · 失败 ${count(result.failed_count)}`;
    $("keptCount").textContent = count(result.kept_rows);
    $("updatedCount").textContent = `填充活动价 ${count(result.updated_rows)} 行`;
    $("removedCount").textContent = count(Number(result.removed_rows || 0) + Number(result.unparseable_rows || 0));
    $("exceptionCount").textContent = `异常行 ${count(result.unparseable_rows)} · 另存明细`;
    $("totalCount").textContent = count(result.total_rows);
    $("resultBadge").textContent = result.preview ? "演示数据" : result.is_complete ? "已完成" : Number(result.success_count) ? "部分完成" : "处理失败";
    $("resultMessage").textContent = result.preview ? "仅展示示例，未读取或生成真实 Excel。"
      : `本批 ${count(result.file_count)} 个文件，完成 ${count(result.success_count)} 个，失败 ${count(result.failed_count)} 个；保留 ${count(result.kept_rows)} 行，折扣剔除 ${count(result.removed_rows)} 行，异常 ${count(result.unparseable_rows)} 行。${result.completion_message || ""}`;
    $("taskBadge").className = `status-pill ${result.is_complete ? "success" : "failed"}`;
    $("taskBadge").textContent = result.preview ? "演示结束" : result.is_complete ? "已完成" : "结果不完整";
    $("statusText").textContent = result.preview ? "界面演示 · 未生成文件" : "结果已保存，原文件保留";
    $("fileResults").replaceChildren();
    (result.files || []).forEach(file => {
      const row = document.createElement("tr"), name = document.createElement("td"), mode = document.createElement("small");
      name.textContent = file.name || fileName(file.source_file || "—"); mode.textContent = MODE_LABELS[file.mode] || file.mode || "—"; name.appendChild(mode); row.appendChild(name);
      const statusCell = document.createElement("td"), status = document.createElement("span"); status.className = `campaign-row-status ${result.preview ? "preview" : file.status === "success" ? "success" : "failed"}`;
      status.textContent = result.preview ? "演示" : file.status === "success" ? "已完成" : "失败"; statusCell.appendChild(status); row.appendChild(statusCell);
      [file.total_rows, file.updated_rows, file.kept_rows, file.removed_rows, file.unparseable_rows].forEach(value => { const cell = document.createElement("td"); cell.textContent = count(value); row.appendChild(cell); });
      const output = document.createElement("td"), actions = document.createElement("div"); actions.className = "campaign-row-actions";
      if (!result.preview && file.processed_file) actions.appendChild(outputButton("筛选 Excel", file.processed_file));
      if (!result.preview && file.audit_file) actions.appendChild(outputButton("异常明细", file.audit_file));
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
      $("processingMode").addEventListener("change", () => { showIssue(); $("inspectionMessage").textContent = "处理方式已更新，可重新检查表格。"; });
      $("discountThreshold").addEventListener("input", () => { $("thresholdLabel").textContent = `${$("discountThreshold").value || "—"}%`; showIssue(); });
      $("campaignOutputDir").addEventListener("input", controls);
      $("clearFilesBtn").addEventListener("click", () => { $("sourcePaths").value = ""; filesChanged(); });
      $("chooseFilesBtn").addEventListener("click", () => prepare(async () => {
        const response = await call("choose_lazada_campaign_files"); if (response.cancelled) return;
        $("sourcePaths").value = [...paths(), ...(response.paths || [])].join("\n"); filesChanged();
      }));
      $("chooseOutputBtn").addEventListener("click", () => prepare(async () => {
        const response = await call("choose_output_dir", $("campaignOutputDir").value.trim());
        if (!response.cancelled) $("campaignOutputDir").value = response.path || "";
      }));
      $("inspectBtn").addEventListener("click", () => prepare(async () => {
        const result = await call("inspect_lazada_campaign_files", payload());
        $("inspectionMessage").textContent = result.files.map(file => file.ok ? `${file.name}：${MODE_LABELS[file.mode] || file.mode}，${count(file.total_rows)} 行，异常 ${count(file.unparseable_rows)} 行。` : `${file.name}：${file.error}`).join("\n");
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
      try {
        const response = await call("get_lazada_campaign_workbook_info");
        if (response.workflow !== "local_excel") throw new Error("请关闭并重新启动工作台，加载本地 Excel 处理后端。");
        preview = Boolean(response.preview); $("previewNotice").hidden = !preview;
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
      snapshot = null;
      ["keptCount", "removedCount", "totalCount"].forEach(id => { $(id).textContent = "0"; });
      $("completedCount").textContent = "完成 0 · 失败 0"; $("updatedCount").textContent = "填充活动价 0 行"; $("exceptionCount").textContent = "异常行 0 · 另存明细";
      emptyResults("处理完成后显示每个文件的结果。"); controls();
    },
    onTaskUpdate(task) { if (task.status === "failed" && !task.result) { $("resultBadge").textContent = "处理失败"; $("resultMessage").textContent = task.error || "处理失败，请查看日志。"; } },
    onTaskRestored(task) {
      const context = task.context || {};
      if (!page.taskContextFilter(context)) return;
      $("sourcePaths").value = context.source_files.join("\n");
      if (["auto", "plain", "invited"].includes(context.mode)) $("processingMode").value = context.mode;
      if (context.threshold !== undefined) { $("discountThreshold").value = String(Number((Number(context.threshold) * 100).toFixed(2))); $("thresholdLabel").textContent = `${$("discountThreshold").value}%`; }
      if (context.output_dir) $("campaignOutputDir").value = context.output_dir;
      renderFiles();
    },
    applyResult: renderResult,
  };
  HQYL.boot(page);
})();
