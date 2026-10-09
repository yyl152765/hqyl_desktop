(() => {
  const $ = HQYL.$;
  const RESULT_PAGE_SIZE = 50;
  let outputDir = "";
  let outputFile = "";
  let rowsPerPage = 500;
  let developers = [];
  let selectedDeveloperIds = new Set();
  let resultRows = [];
  let resultPage = 1;
  let loadingDevelopers = false;
  let resultMode = "inventory";

  function updateQueryMode() {
    const coverage = $("skuQueryMode").value === "missing_warehouses";
    $("livenessField").hidden = !coverage;
    $("skuDateFields").hidden = coverage;
    $("coverageRuleNote").hidden = !coverage;
    $("inventoryRuleNote").hidden = coverage;
    $("queryModeTitle").textContent = coverage ? "爆款／旺款缺失仓库" : "SKU＋仓库明细";
    $("queryModeSummary").textContent = coverage ? "全部东南亚仓，不含中转仓" : "所有仓库，包含有库存、未发货或在途的记录";
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function visibleDevelopers() {
    const keyword = $("developerSearch").value.trim().toLowerCase();
    return developers.filter((developer) => !keyword || developer.name.toLowerCase().includes(keyword));
  }

  function updateDeveloperSummary() {
    const selectedCount = selectedDeveloperIds.size;
    const total = developers.length;
    $("developerSelectionSummary").textContent = total
      ? `已选择 ${selectedCount} / ${total} 人`
      : "请先加载开发人员";
  }

  function renderDevelopers() {
    const visible = visibleDevelopers();
    if (!developers.length) {
      $("developerChecklist").innerHTML = '<div class="developer-empty">加载后可搜索并勾选开发人员</div>';
      updateDeveloperSummary();
      return;
    }
    if (!visible.length) {
      $("developerChecklist").innerHTML = '<div class="developer-empty">没有匹配的开发人员</div>';
      updateDeveloperSummary();
      return;
    }
    $("developerChecklist").innerHTML = visible.map((developer) => `
      <label class="developer-option">
        <input type="checkbox" value="${escapeHtml(developer.id)}"${selectedDeveloperIds.has(developer.id) ? " checked" : ""} />
        <span>${escapeHtml(developer.name)}</span>
      </label>`).join("");
    $("developerChecklist").querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedDeveloperIds.add(checkbox.value);
        else selectedDeveloperIds.delete(checkbox.value);
        updateDeveloperSummary();
      });
    });
    updateDeveloperSummary();
  }

  function setDeveloperLoading(loading) {
    loadingDevelopers = loading;
    $("loadDevelopersBtn").disabled = loading;
    $("loadDevelopersBtn").textContent = loading ? "正在加载..." : (developers.length ? "重新加载开发人员" : "加载开发人员");
    $("developerSearch").disabled = loading || !developers.length;
    $("selectAllDevelopersBtn").disabled = loading || !developers.length;
    $("clearDevelopersBtn").disabled = loading || !developers.length;
  }

  async function loadDevelopers() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", loadDevelopers);
      return;
    }
    setDeveloperLoading(true);
    $("developerChecklist").innerHTML = '<div class="developer-empty">正在从马帮读取开发人员...</div>';
    try {
      const response = await HQYL.api().get_sku_inventory_developers({ account_id: account.id });
      if (!response.ok) throw new Error(response.error || "开发人员加载失败");
      developers = Array.isArray(response.developers) ? response.developers : [];
      const availableIds = new Set(developers.map((developer) => developer.id));
      selectedDeveloperIds = new Set(Array.from(selectedDeveloperIds).filter((id) => availableIds.has(id)));
      $("developerSearch").value = "";
      renderDevelopers();
      HQYL.showToast(`已加载 ${developers.length} 名开发人员`);
    } catch (error) {
      developers = [];
      selectedDeveloperIds.clear();
      $("developerChecklist").innerHTML = `<div class="developer-empty error-text">${escapeHtml(error.message || String(error))}</div>`;
      updateDeveloperSummary();
      HQYL.showToast(error.message || String(error));
    } finally {
      setDeveloperLoading(false);
    }
  }

  async function runQuery() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runQuery);
      return;
    }
    if (!selectedDeveloperIds.size) {
      HQYL.showToast("请至少选择一名开发人员");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备查询 ${selectedDeveloperIds.size} 名开发人员的${$("skuQueryMode").value === "missing_warehouses" ? "爆款／旺款缺失仓库" : "SKU仓库库存"}`);
      return HQYL.api().start_sku_inventory_query({
        query_mode: $("skuQueryMode").value,
        liveness_types: $("skuLivenessTypes").value.split(","),
        account_id: account.id,
        developer_ids: Array.from(selectedDeveloperIds),
        start_date: $("skuCreatedStartDate").value,
        end_date: $("skuCreatedEndDate").value,
        rows_per_page: rowsPerPage,
        output_dir: outputDir,
      });
    });
  }

  function renderResultPage() {
    const totalPages = Math.max(1, Math.ceil(resultRows.length / RESULT_PAGE_SIZE));
    resultPage = Math.max(1, Math.min(resultPage, totalPages));
    const start = (resultPage - 1) * RESULT_PAGE_SIZE;
    const rows = resultRows.slice(start, start + RESULT_PAGE_SIZE);
    const coverage = resultMode === "missing_warehouses";
    $("inventoryTableHead").hidden = coverage;
    $("coverageTableHead").hidden = !coverage;
    $("skuInventoryTable").classList.toggle("sku-coverage-table", coverage);
    $("skuInventoryTableBody").innerHTML = coverage ? rows.map((row) => `<tr>
      <td>${escapeHtml(row.developer_name)}</td>
      <td>${escapeHtml(row.sku)}</td>
      <td title="${escapeHtml(row.product_name)}">${escapeHtml(row.product_name)}</td>
      <td>${escapeHtml(row.liveness_name)}</td>
      <td class="missing-warehouses">${escapeHtml(row.missing_warehouse_names || "已覆盖全部目标仓库")}</td>
      <td class="number-cell">${escapeHtml(row.missing_warehouse_count)}</td>
      <td class="number-cell">${escapeHtml(row.covered_warehouse_count)}</td>
    </tr>`).join("") : rows.map((row) => `<tr>
      <td title="${escapeHtml(row.developer_name)}">${escapeHtml(row.developer_name)}</td>
      <td title="${escapeHtml(row.sku)}">${escapeHtml(row.sku)}</td>
      <td title="${escapeHtml(row.product_name)}">${escapeHtml(row.product_name)}</td>
      <td>${escapeHtml(row.created_at || "--")}</td>
      <td title="${escapeHtml(row.warehouse_name)}">${escapeHtml(row.warehouse_name)}</td>
      <td>${escapeHtml(row.warehouse_id || "--")}</td>
      <td class="number-cell">${escapeHtml(row.available_inventory)}</td>
      <td class="number-cell">${escapeHtml(row.forecast_daily_sales)}</td>
      <td class="number-cell">${escapeHtml(row.current_sales_days)}</td>
      <td class="number-cell">${escapeHtml(row.unshipped_count)}</td>
      <td class="number-cell">${escapeHtml(row.transit_inventory)}</td>
    </tr>`).join("");
    $("skuInventoryEmpty").hidden = resultRows.length > 0;
    $("skuInventoryEmpty").textContent = coverage ? "所选开发员名下没有符合条件的爆款／旺款SKU" : "没有符合条件的库存、未发货或在途仓库记录";
    $("skuInventoryPagination").hidden = resultRows.length <= RESULT_PAGE_SIZE;
    $("skuInventoryPageText").textContent = `第 ${resultPage} / ${totalPages} 页`;
    $("skuInventoryPrevBtn").disabled = resultPage <= 1;
    $("skuInventoryNextBtn").disabled = resultPage >= totalPages;
  }

  const page = {
    key: "sku_inventory_query",
    taskKey: "sku_inventory_query",
    title: "SKU库存与可售天数查询",
    async init(info) {
      const settings = info.settings || {};
      const dates = info.date_range || {};
      outputDir = settings.output_dir || "";
      rowsPerPage = Number(settings.rows_per_page || 500);
      $("skuCreatedStartDate").value = dates.start_date || "";
      $("skuCreatedEndDate").value = dates.end_date || "";
      $("skuQueryMode").addEventListener("change", updateQueryMode);
      updateQueryMode();
      $("skuInventoryForm").addEventListener("submit", (event) => { event.preventDefault(); runQuery(); });
      $("loadDevelopersBtn").addEventListener("click", loadDevelopers);
      $("developerSearch").addEventListener("input", renderDevelopers);
      $("selectAllDevelopersBtn").addEventListener("click", () => {
        visibleDevelopers().forEach((developer) => selectedDeveloperIds.add(developer.id));
        renderDevelopers();
      });
      $("clearDevelopersBtn").addEventListener("click", () => {
        selectedDeveloperIds.clear();
        renderDevelopers();
      });
      $("skuInventoryOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
      $("skuInventoryPrevBtn").addEventListener("click", () => { resultPage -= 1; renderResultPage(); });
      $("skuInventoryNextBtn").addEventListener("click", () => { resultPage += 1; renderResultPage(); });
      setDeveloperLoading(false);
    },
    setRunning(running) {
      $("skuInventoryRunBtn").disabled = running;
      $("loadDevelopersBtn").disabled = running || loadingDevelopers;
      $("developerSearch").disabled = running || loadingDevelopers || !developers.length;
      $("selectAllDevelopersBtn").disabled = running || loadingDevelopers || !developers.length;
      $("clearDevelopersBtn").disabled = running || loadingDevelopers || !developers.length;
      $("skuQueryMode").disabled = running;
      $("skuLivenessTypes").disabled = running;
    },
    resetResult() {
      outputFile = "";
      resultRows = [];
      resultPage = 1;
      $("skuInventoryOpenOutputBtn").disabled = true;
      $("skuInventoryResult").hidden = true;
      $("skuCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("warehouseCount").textContent = "0 个仓库";
    },
    applyResult(result) {
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      resultRows = Array.isArray(result.rows) ? result.rows : [];
      resultMode = result.query_mode || "inventory";
      const coverage = resultMode === "missing_warehouses";
      resultPage = 1;
      $("skuCount").textContent = String(result.sku_count ?? 0);
      $("recordCount").textContent = String(result.record_count ?? 0);
      $("warehouseCount").textContent = `${result.warehouse_count ?? 0} 个仓库`;
      $("outputFile").textContent = outputFile || "未生成";
      $("skuInventoryOpenOutputBtn").disabled = !outputFile && !outputDir;
      $("skuInventoryResult").hidden = false;
      $("resultSummary").textContent = coverage
        ? `${result.developer_count ?? selectedDeveloperIds.size} 名开发人员，${result.sku_count ?? 0} 个爆款／旺款SKU，其中 ${result.missing_sku_count ?? 0} 个存在缺失仓库`
        : `${result.developer_count ?? selectedDeveloperIds.size} 名开发人员，${result.sku_count ?? 0} 个SKU，共 ${result.record_count ?? 0} 条SKU＋仓库记录`;
      $("warehouseScopeNote").hidden = !coverage;
      $("warehouseScopeNote").textContent = coverage
        ? `本次对比的东南亚非中转仓（${result.warehouse_count ?? 0} 个）：${(result.scope_warehouses || []).map((warehouse) => warehouse.name).join("、")}` : "";
      const unresolved = result.unclassified_warehouses || [];
      $("warehouseScopeWarning").hidden = !coverage || !unresolved.length;
      $("warehouseScopeWarning").textContent = unresolved.length
        ? `待确认归属（尚未纳入对比）：${unresolved.map((warehouse) => warehouse.name).join("、")}。请在马帮完善仓库所属国家后重新查询。` : "";
      $("resultLimitNote").textContent = result.preview_truncated
        ? `页面展示前 ${result.preview_limit || resultRows.length} 条，Excel为完整结果`
        : `页面展示全部 ${resultRows.length} 条`;
      renderResultPage();
    },
    onAccountsChanged() {
      developers = [];
      selectedDeveloperIds.clear();
      $("developerSearch").value = "";
      renderDevelopers();
      setDeveloperLoading(false);
    },
  };

  HQYL.boot(page);
})();
