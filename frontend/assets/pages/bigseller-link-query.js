(() => {
  const $ = HQYL.$;
  const MAX_SKU_COUNT = 50000;
  const PAGE_SIZE = 50;
  const PREVIEW_LIMIT = 500;
  let running = false;
  let outputFile = "";
  let resultOutputDir = "";
  let resultRows = [];
  let failedSkus = [];
  let resultPage = 1;
  let confirmed = 0;
  let total = 0;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function skuItems() {
    return Array.from(new Set($("skuText").value.split(/[\r\n,，;；\t]+/).map((sku) => sku.trim()).filter(Boolean)));
  }

  function updateSkuCount() {
    const count = skuItems().length;
    $("skuCount").textContent = String(count);
    $("skuInputSummary").textContent = count > MAX_SKU_COUNT ? `已输入 ${count} 个唯一 SKU，超过 ${MAX_SKU_COUNT} 个上限，请分批查询` : `已输入 ${count} 个唯一 SKU`;
  }

  function setProgress(done, count, message) {
    total = Math.max(0, Number(count) || 0);
    confirmed = Math.min(total, Math.max(0, Number(done) || 0));
    const percent = total ? Math.floor(confirmed / total * 100) : 0;
    $("progressCount").textContent = `${confirmed} / ${total}`;
    $("queryProgressBar").style.width = `${percent}%`;
    $("queryProgress").setAttribute("aria-valuenow", String(percent));
    $("queryProgress").setAttribute("aria-valuetext", `已确认 ${confirmed} / ${total} 个 SKU`);
    $("progressText").textContent = message;
  }

  async function chooseOutputDir() {
    try {
      const result = await HQYL.api().choose_output_dir($("outputDir").value.trim());
      if (result.ok) $("outputDir").value = result.path;
      else if (!result.cancelled) HQYL.showToast(result.error || "选择目录失败");
    } catch (error) { HQYL.showToast(error.message || String(error)); }
  }

  async function openResultFile() {
    if (!outputFile) return;
    try {
      const result = await HQYL.api().open_path(outputFile);
      if (!result.ok) HQYL.showToast(result.error || "打开 Excel 失败");
    } catch (error) { HQYL.showToast(error.message || String(error)); }
  }

  async function runQuery() {
    if (running) return;
    const skus = skuItems();
    if (!skus.length || skus.length > MAX_SKU_COUNT) {
      HQYL.showToast(skus.length ? `每次最多查询 ${MAX_SKU_COUNT} 个不同 SKU，请分批输入` : "请至少输入一个 SKU");
      $("skuText").focus();
      return;
    }
    const outputDir = $("outputDir").value.trim();
    const site = $("site").value;
    const siteLabel = $("site").selectedOptions[0].textContent;
    if (!outputDir) { HQYL.showToast("请选择输出目录"); return; }
    if (!HQYL.activeAccount("bigseller")) { HQYL.openAccountDialog("bigseller", runQuery); return; }
    // Preserve the last result if the bridge rejects a new task before it starts.
    const previous = { resultPage, confirmed, total };
    const previousResult = lastResult;
    const task = await HQYL.startTask(() => {
      HQYL.appendLog(`准备查询 ${skus.length} 个 SKU，国家/地区：${siteLabel}；先查在售，无匹配再查下架；按上架时间升序取首条商品`);
      return HQYL.api().start_bigseller_link_query({ sku_text: skus.join("\n"), output_dir: outputDir, site });
    });
    if (!task) {
      if (previousResult) {
        page.applyResult(previousResult);
        resultPage = previous.resultPage;
        renderResultPage();
      }
      setProgress(previous.confirmed, previous.total, previousResult ? "任务未启动，已保留上次结果；请查看日志后重试" : "任务未启动，请查看日志后重试");
      $("taskBadge").className = "status-pill failed";
      $("taskBadge").textContent = "启动失败";
      $("statusText").textContent = previousResult ? "任务未启动，已保留上次结果" : "任务未启动";
    }
  }

  function safeLink(value) {
    const text = typeof value === "string" ? value.trim() : "";
    if (!text || text.length > 2048 || /[\s\u0000-\u001f]/.test(text)) return "";
    try {
      const url = new URL(text);
      if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.port) return "";
      if (!/^(?:www\.)?shopee\.(?:co\.id|ph|com\.my|vn|co\.th|sg|tw|com\.br|com\.mx|com\.co|cl)$/.test(url.hostname)) return "";
      return url.pathname.replace(/\//g, "") ? text : "";
    } catch (_error) { return ""; }
  }

  function renderResultPage() {
    const pages = Math.max(1, Math.ceil(resultRows.length / PAGE_SIZE));
    resultPage = Math.max(1, Math.min(resultPage, pages));
    $("resultTableBody").innerHTML = resultRows.slice((resultPage - 1) * PAGE_SIZE, resultPage * PAGE_SIZE).map((row) => {
      const states = { matched: ["已匹配", "success"], not_found: ["未找到商品", "no_data"], failed: ["查询失败", "failed"] };
      const [label, className] = states[row.status] || ["未知状态", "failed"];
      const url = safeLink(row.product_url);
      const scope = { live: "在售", unlisted: "下架" }[row.listing_scope] || "—";
      return `<tr>
        <td title="${escapeHtml(row.sku)}">${escapeHtml(row.sku)}</td>
        <td title="${escapeHtml(row.shop_name)}">${escapeHtml(row.shop_name || "—")}</td>
        <td>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(url)}">查看商品 ↗</a>` : "—"}</td>
        <td>${escapeHtml(row.listed_time || "—")}</td>
        <td>${escapeHtml(row.item_id || "—")}</td>
        <td>${scope}</td>
        <td><span class="collection-state ${className}">${label}</span></td>
        <td title="${escapeHtml(row.message)}">${escapeHtml(row.message || "—")}</td>
      </tr>`;
    }).join("");
    $("resultEmpty").hidden = resultRows.length > 0;
    $("resultPrevBtn").disabled = resultPage <= 1;
    $("resultNextBtn").disabled = resultPage >= pages;
    $("resultPageText").textContent = `第 ${resultPage} / ${pages} 页`;
  }

  let lastResult = null;
  const page = {
    key: "bigseller_link_query", taskKey: "bigseller_link_query", title: "BS 抓取链接",
    async init(info) {
      $("outputDir").value = info.settings?.output_dir || "";
      $("linkQueryForm").addEventListener("submit", (event) => { event.preventDefault(); return runQuery(); });
      $("skuText").addEventListener("input", updateSkuCount);
      $("clearSkuBtn").addEventListener("click", () => { $("skuText").value = ""; updateSkuCount(); $("skuText").focus(); });
      $("chooseDirBtn").addEventListener("click", chooseOutputDir);
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || resultOutputDir));
      $("openFileBtn").addEventListener("click", openResultFile);
      $("retryFailedBtn").addEventListener("click", () => {
        if (running || !failedSkus.length) return;
        $("skuText").value = failedSkus.join("\n");
        if (lastResult?.site) $("site").value = lastResult.site;
        updateSkuCount();
        $("skuText").focus();
        HQYL.showToast(`已填入 ${failedSkus.length} 个失败 SKU，可重新查询`);
      });
      $("resultPrevBtn").addEventListener("click", () => { resultPage -= 1; renderResultPage(); });
      $("resultNextBtn").addEventListener("click", () => { resultPage += 1; renderResultPage(); });
      updateSkuCount();
    },
    setRunning(value) {
      running = value;
      ["runBtn", "skuText", "clearSkuBtn", "outputDir", "chooseDirBtn", "retryFailedBtn", "site"].forEach((id) => { $(id).disabled = value; });
      $("runBtn").textContent = value ? "正在查询…" : "开始查询并导出";
    },
    resetResult() {
      outputFile = ""; resultOutputDir = ""; resultRows = []; failedSkus = []; resultPage = 1; lastResult = null;
      $("queryResult").hidden = true;
      $("previewNotice").hidden = true;
      $("failureNotice").hidden = true;
      $("retryFailedBtn").hidden = true;
      $("openOutputBtn").disabled = true;
      $("openFileBtn").disabled = true;
      $("matchedCount").textContent = "0";
      $("unmatchedCount").textContent = "0 / 0";
      $("outputFile").textContent = "未生成";
      setProgress(0, skuItems().length, "正在准备查询，请稍候…");
    },
    onTaskUpdate(task) {
      let done = confirmed;
      let count = Number(task.context?.sku_count || total);
      for (const line of (task.logs || []).slice().reverse()) {
        const match = String(line).match(/\[BS链接进度\s+(\d+)\/(\d+)\]/);
        if (match) { done = Number(match[1]); count = Number(match[2]); break; }
      }
      if (["running", "pending"].includes(task.status)) {
        const latest = String((task.logs || []).at(-1) || "");
        setProgress(done, count, latest.includes("等待") || latest.includes("继续查询下架") ? latest : "正在查询：先查在售，无匹配再查下架");
      } else if (task.status === "failed" && !task.result) {
        setProgress(done, count, "任务失败，请查看日志后重试");
      }
    },
    applyResult(result) {
      lastResult = result;
      outputFile = result.output_file || "";
      resultOutputDir = result.output_dir || "";
      resultRows = (Array.isArray(result.rows) ? result.rows : []).slice(0, PREVIEW_LIMIT);
      failedSkus = Array.isArray(result.failed_skus) ? result.failed_skus : [];
      resultPage = 1;
      const count = Number(result.sku_count ?? resultRows.length);
      const matched = Number(result.matched_count || 0);
      const notFound = Number(result.not_found_count || 0);
      const failed = Number(result.failed_count || 0);
      const incomplete = result.is_complete === false || failed > 0;
      const message = result.completion_message || (incomplete ? "查询结果不完整" : "查询完成");
      $("skuCount").textContent = String(count);
      $("matchedCount").textContent = String(matched);
      $("unmatchedCount").textContent = `${notFound} / ${failed}`;
      $("outputFile").textContent = outputFile || (result.is_preview ? "演示模式，不生成文件" : "未生成");
      $("openOutputBtn").disabled = !outputFile && !resultOutputDir;
      $("openFileBtn").disabled = !outputFile;
      $("queryResult").hidden = false;
      $("previewNotice").hidden = !result.is_preview;
      $("failureNotice").hidden = !incomplete;
      $("failureNotice").textContent = message;
      $("retryFailedBtn").hidden = !failedSkus.length;
      $("taskBadge").className = `status-pill ${incomplete ? "failed" : "success"}`;
      $("taskBadge").textContent = incomplete ? "结果不完整" : result.is_preview ? "演示完成" : "已完成";
      $("statusText").textContent = message;
      const scopes = Number.isFinite(result.live_matched_count) && Number.isFinite(result.unlisted_matched_count)
        ? `（在售 ${result.live_matched_count}，下架 ${result.unlisted_matched_count}）` : "";
      const resultSite = result.site_name ? `${result.site_name}（${result.site}） · ` : result.site ? `${result.site} · ` : "";
      $("resultSummary").textContent = `${resultSite}${count} 个 SKU：已匹配 ${matched}${scopes}，未找到商品 ${notFound}，查询失败 ${failed}。`;
      $("resultLimitNote").textContent = result.preview_limited
        ? `预览前 ${resultRows.length} 条${outputFile ? `，Excel 包含全部 ${count} 条` : ""}`
        : `共 ${resultRows.length} 条，每页 ${PAGE_SIZE} 条`;
      setProgress(result.confirmed_count ?? matched + notFound, count, message);
      renderResultPage();
    },
  };
  HQYL.boot(page);
})();
