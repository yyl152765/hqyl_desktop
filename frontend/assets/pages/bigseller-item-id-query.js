(() => {
  const $ = HQYL.$;
  const RESULT_PAGE_SIZE = 50;
  const PREVIEW_LIMIT = 500;
  const MAX_SKUS = 5000;
  let outputDir = "";
  let resultOutputDir = "";
  let outputFile = "";
  let resultRows = [];
  let resultPage = 1;
  let running = false;
  let progressCompleted = 0;
  let progressTotal = 0;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function skuItems() {
    return Array.from(new Set($("skuText").value.split(/[\r\n,，;；\t]+/).map((sku) => sku.trim()).filter(Boolean)));
  }

  function updateSkuCount() {
    const count = skuItems().length;
    $("skuCount").textContent = String(count);
    $("skuInputSummary").textContent = count > MAX_SKUS ? `已输入 ${count} 个唯一 SKU，超过 ${MAX_SKUS} 个上限，请分批查询` : `已输入 ${count} 个唯一 SKU`;
  }

  function setProgress(completed, total, message) {
    progressTotal = Math.max(0, Number(total) || 0);
    progressCompleted = Math.min(progressTotal, Math.max(0, Number(completed) || 0));
    const percent = progressTotal ? Math.round(progressCompleted / progressTotal * 100) : 0;
    $("progressCount").textContent = `${progressCompleted} / ${progressTotal}`;
    $("queryProgressBar").style.width = `${percent}%`;
    $("queryProgress").setAttribute("aria-valuenow", String(percent));
    $("queryProgress").setAttribute("aria-valuetext", `已处理 ${progressCompleted} / ${progressTotal} 个 SKU`);
    $("progressText").textContent = message;
  }

  async function chooseOutputDir() {
    try {
      const result = await HQYL.api().choose_output_dir($("outputDir").value.trim() || outputDir);
      if (result.ok) {
        outputDir = result.path;
        $("outputDir").value = result.path;
      } else if (!result.cancelled) {
        HQYL.showToast(result.error || "选择目录失败");
      }
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function openResultFile() {
    if (!outputFile) return;
    try {
      const result = await HQYL.api().open_path(outputFile);
      if (!result.ok) HQYL.showToast(result.error || "打开 Excel 失败");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function runQuery() {
    if (running) return;
    const skus = skuItems();
    if (!skus.length) {
      HQYL.showToast("请至少输入一个 SKU");
      $("skuText").focus();
      return;
    }
    if (skus.length > MAX_SKUS) {
      HQYL.showToast(`每次最多查询 ${MAX_SKUS} 个唯一 SKU，请分批输入`);
      return;
    }
    const selectedOutputDir = $("outputDir").value.trim() || outputDir;
    if (!selectedOutputDir) {
      HQYL.showToast("请选择输出目录");
      return;
    }
    if (!HQYL.activeAccount("bigseller")) {
      HQYL.openAccountDialog("bigseller", runQuery);
      return;
    }
    outputDir = selectedOutputDir;
    const task = await HQYL.startTask(() => {
      HQYL.appendLog(`准备查询 ${skus.length} 个 SKU：SKU（含子SKU） / Fuzzy Search / Views 降序，取第一条 Item ID`);
      return HQYL.api().start_bigseller_item_id_query({ sku_text: skus.join("\n"), output_dir: selectedOutputDir });
    });
    if (!task) setProgress(progressCompleted, progressTotal, "任务启动失败，请查看运行日志后重试");
  }

  function rowState(row) {
    if (["matched", "success", "found"].includes(row.status)) return { label: "已匹配", className: "success" };
    if (row.status === "not_found") return { label: "未匹配", className: "no_data" };
    if (["failed", "error"].includes(row.status)) return { label: "查询失败", className: "failed" };
    return { label: "未知状态", className: "" };
  }

  function renderResultPage() {
    const pageCount = Math.max(1, Math.ceil(resultRows.length / RESULT_PAGE_SIZE));
    resultPage = Math.max(1, Math.min(resultPage, pageCount));
    const rows = resultRows.slice((resultPage - 1) * RESULT_PAGE_SIZE, resultPage * RESULT_PAGE_SIZE);
    $("resultTableBody").innerHTML = rows.map((row) => {
      const status = rowState(row);
      return `<tr>
        <td title="${escapeHtml(row.sku)}">${escapeHtml(row.sku)}</td>
        <td>${escapeHtml(row.item_id || "—")}</td>
        <td><span class="collection-state ${status.className}">${status.label}</span></td>
        <td title="${escapeHtml(row.message)}">${escapeHtml(row.message || "—")}</td>
      </tr>`;
    }).join("");
    $("resultEmpty").hidden = resultRows.length > 0;
    $("resultPrevBtn").disabled = resultPage <= 1;
    $("resultNextBtn").disabled = resultPage >= pageCount;
    $("resultPageText").textContent = `第 ${resultPage} / ${pageCount} 页`;
  }

  const page = {
    key: "bigseller_item_id_query",
    taskKey: "bigseller_item_id_query",
    title: "BS 商品ID查询",
    async init(info) {
      outputDir = info.settings?.output_dir || "";
      $("outputDir").value = outputDir;
      $("itemIdQueryForm").addEventListener("submit", (event) => { event.preventDefault(); return runQuery(); });
      $("skuText").addEventListener("input", updateSkuCount);
      $("clearSkuBtn").addEventListener("click", () => { $("skuText").value = ""; updateSkuCount(); $("skuText").focus(); });
      $("chooseDirBtn").addEventListener("click", chooseOutputDir);
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || resultOutputDir));
      $("openFileBtn").addEventListener("click", openResultFile);
      $("resultPrevBtn").addEventListener("click", () => { resultPage -= 1; renderResultPage(); });
      $("resultNextBtn").addEventListener("click", () => { resultPage += 1; renderResultPage(); });
      updateSkuCount();
    },
    setRunning(value) {
      running = value;
      $("runBtn").disabled = value;
      $("runBtn").textContent = value ? "正在查询…" : "开始查询并导出";
      $("skuText").disabled = value;
      $("clearSkuBtn").disabled = value;
      $("outputDir").disabled = value;
      $("chooseDirBtn").disabled = value;
    },
    resetResult() {
      outputFile = "";
      resultOutputDir = "";
      resultRows = [];
      resultPage = 1;
      $("queryResult").hidden = true;
      $("previewNotice").hidden = true;
      $("failureNotice").hidden = true;
      $("openOutputBtn").disabled = true;
      $("openFileBtn").disabled = true;
      $("matchedCount").textContent = "0";
      $("unmatchedCount").textContent = "0 / 0";
      $("outputFile").textContent = "未生成";
      setProgress(0, skuItems().length, "正在准备查询，请稍候…");
    },
    onTaskUpdate(task) {
      let completed = progressCompleted;
      let total = Number(task.context?.sku_count || progressTotal);
      if (task.progress && Number.isFinite(Number(task.progress.total))) {
        total = Number(task.progress.total);
        completed = Number(task.progress.completed || 0);
      } else {
        for (const line of (task.logs || []).slice().reverse()) {
          const match = String(line).match(/\[BS进度\s+(\d+)\/(\d+)\]/);
          if (match) { completed = Number(match[1]); total = Number(match[2]); break; }
        }
      }
      if (task.status === "running" || task.status === "pending") {
        setProgress(completed, total, completed ? "正在逐条查询；详细结果见运行日志" : "正在登录或准备查询，请稍候…");
        if (!$("skuText").value.trim() && total) $("skuCount").textContent = String(total);
      } else if (task.status === "failed") {
        setProgress(completed, total, "任务失败，请查看运行日志后重试");
      }
    },
    applyResult(result) {
      outputFile = result.output_file || "";
      resultOutputDir = result.output_dir || "";
      const allRows = Array.isArray(result.rows) ? result.rows : [];
      resultRows = allRows.slice(0, PREVIEW_LIMIT);
      resultPage = 1;
      const total = Number(result.sku_count ?? resultRows.length);
      const matched = Number(result.matched_count || 0);
      const notFound = Number(result.not_found_count || 0);
      const failed = Number(result.failed_count || 0);
      if (failed) {
        $("taskBadge").className = "status-pill failed";
        $("taskBadge").textContent = failed === total ? "查询失败" : "部分失败";
        $("statusText").textContent = "存在查询失败的 SKU，请查看日志";
      }
      $("skuCount").textContent = String(total);
      $("matchedCount").textContent = String(matched);
      $("unmatchedCount").textContent = `${notFound} / ${failed}`;
      $("outputFile").textContent = outputFile || (result.is_preview ? "演示模式，不生成文件" : "未生成");
      $("openOutputBtn").disabled = !outputFile && !resultOutputDir;
      $("openFileBtn").disabled = !outputFile;
      $("queryResult").hidden = false;
      $("previewNotice").hidden = !result.is_preview;
      $("failureNotice").hidden = !failed;
      $("failureNotice").textContent = `有 ${failed} 个 SKU 查询失败，不应视为未匹配商品；对应 Item ID 留空，请根据日志处理后重新查询。`;
      $("resultSummary").textContent = `${total} 个 SKU：已匹配 ${matched}，未匹配 ${notFound}，查询失败 ${failed}。Excel 仅包含 SKU、Item ID 两列。`;
      $("resultLimitNote").textContent = result.preview_limited || allRows.length > PREVIEW_LIMIT
        ? result.is_preview ? `演示预览前 ${resultRows.length} 条，不生成 Excel` : `预览前 ${resultRows.length} 条，Excel 包含全部 ${total} 条`
        : `共 ${resultRows.length} 条，每页 ${RESULT_PAGE_SIZE} 条`;
      setProgress(total, total, result.is_preview ? "界面演示已完成，未执行真实查询" : failed ? "存在查询失败的 SKU，请查看日志" : "查询完成");
      renderResultPage();
    },
  };

  HQYL.boot(page);
})();
