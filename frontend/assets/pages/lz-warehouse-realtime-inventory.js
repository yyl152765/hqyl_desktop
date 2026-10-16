(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";
  let resultOutputDir = "";
  let outputSaveQueue = Promise.resolve(true);
  let savedOutputDir = "";
  let outputRevision = 0;
  let accountId = "";
  let accountGeneration = 0;
  let initialized = false;
  let outputInitialized = false;
  let ready = false;
  let starting = false;
  let running = false;
  let loadingWarehouses = false;
  let warehousesLoaded = false;
  let warehouseNames = [];
  let selectedWarehouses = new Set();
  let previewExports = [];
  let pendingWarehouseSelection = null;
  const DEFAULT_WAREHOUSES = ["泰国TH04仓", "泰国TH05仓", "泰国TH06仓"];
  const TASK_KEYS = ["lz_warehouse_inventory"];
  const BACKEND_UPDATE_HINT = "后台版本尚未更新，请关闭整个工作台后重新启动；刷新页面无法更新后台";

  function inventoryErrorMessage(error) {
    const message = String(error?.message || error || "请求失败");
    return /get_lz_warehouse_inventory_(?:info|warehouses)\(\)/.test(message)
      && /\btakes\b[\s\S]*\bpositional arguments?\b[\s\S]*\bgiven\b/.test(message)
      ? BACKEND_UPDATE_HINT : message;
  }

  function isCurrentAccount(generation, id) {
    return generation === accountGeneration && id === accountId
      && id === String(HQYL.activeAccount("mabang")?.id || "");
  }

  function clearAccountView() {
    if (HQYL.state.pollTimer) window.clearInterval(HQYL.state.pollTimer);
    HQYL.state.pollTimer = 0;
    HQYL.state.taskId = "";
    HQYL.state.outputFile = "";
    ready = false;
    starting = false;
    running = false;
    loadingWarehouses = false;
    warehousesLoaded = false;
    warehouseNames = [];
    selectedWarehouses.clear();
    pendingWarehouseSelection = null;
    $("lzWarehouseSearch").value = "";
    $("logBox").textContent = "";
    $("taskBadge").className = "status-pill idle";
    $("taskBadge").textContent = "待运行";
    $("statusText").textContent = "等待任务启动";
    page.resetResult();
    renderWarehouses();
  }

  async function switchAccount(force = false, restoreTask = true) {
    const account = HQYL.activeAccount("mabang");
    const id = String(account?.id || "");
    if (!force && id === accountId) return;
    accountId = id;
    const generation = ++accountGeneration;
    clearAccountView();
    $("lzBindAccountBtn").hidden = !!id;
    if (!id) {
      $("lzAccountNote").textContent = "请先绑定并选择马帮账号，再加载该账号有权限的仓库。";
      $("lzWarehouseStatus").textContent = "等待选择马帮账号。";
      return;
    }
    $("lzAccountNote").textContent = `正在加载当前账号 ${account.name || account.username || ""}…`;
    try {
      const info = await prepareAccount(generation, id);
      if (!info || !isCurrentAccount(generation, id)) return;
      if (!outputInitialized) {
        if (!outputRevision) {
          outputDir = info.output_dir || "";
          savedOutputDir = outputDir;
          $("lzOutputDir").value = outputDir;
        }
        outputInitialized = true;
      }
      await loadWarehouses(generation, id);
      if (restoreTask && isCurrentAccount(generation, id)) {
        await HQYL.restoreLatestTaskFromKeys(TASK_KEYS, (context) => isCurrentAccount(generation, id) && page.taskContextFilter(context));
      }
    } catch (error) {
      if (!isCurrentAccount(generation, id)) return;
      const message = inventoryErrorMessage(error);
      $("lzAccountNote").textContent = message;
      HQYL.showToast(message);
    }
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function saveOutputDir(force = false) {
    const value = $("lzOutputDir").value.trim();
    const operation = outputSaveQueue.then(async () => {
      if (!force && value === savedOutputDir) return true;
      try {
        const result = await HQYL.api().save_scoped_output_dirs({ tool: "lz_warehouse_inventory", output_dir: value });
        if (!result.ok) throw new Error(result.error || "输出目录保存失败");
        savedOutputDir = value;
        return true;
      } catch (error) { HQYL.showToast(error.message || String(error)); return false; }
    });
    outputSaveQueue = operation;
    return operation;
  }

  function visibleWarehouses() {
    const keyword = $("lzWarehouseSearch").value.trim().toLowerCase();
    return warehouseNames.filter((name) => !keyword || name.toLowerCase().includes(keyword));
  }

  function updateControls() {
    const busy = running || starting;
    $("lzRunBtn").disabled = busy || !ready || loadingWarehouses || !selectedWarehouses.size;
    $("lzOutputDir").disabled = busy;
    $("lzChooseDirBtn").disabled = busy;
    $("lzRefreshWarehousesBtn").disabled = busy || loadingWarehouses || !ready;
    $("lzWarehouseSearch").disabled = busy || loadingWarehouses || !warehouseNames.length;
    $("lzSelectWarehousesBtn").disabled = busy || loadingWarehouses || !warehouseNames.length;
    $("lzClearWarehousesBtn").disabled = busy || loadingWarehouses || !warehouseNames.length;
    $("lzWarehouseChecklist").querySelectorAll("input").forEach((input) => { input.disabled = busy || loadingWarehouses; });
    $("lzWarehouseSelection").textContent = `已选择 ${selectedWarehouses.size} / ${warehouseNames.length} 个仓库`;
  }

  function renderWarehouses() {
    const names = visibleWarehouses();
    $("lzWarehouseChecklist").innerHTML = names.length
      ? names.map((name) => `<label class="developer-option" title="${escapeHtml(name)}"><input type="checkbox" value="${escapeHtml(name)}"${selectedWarehouses.has(name) ? " checked" : ""} /><span>${escapeHtml(name)}</span></label>`).join("")
      : '<div class="developer-empty">没有匹配的仓库，可刷新仓库列表重试</div>';
    $("lzWarehouseChecklist").querySelectorAll("input").forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) selectedWarehouses.add(input.value);
        else selectedWarehouses.delete(input.value);
        $("warehouseCount").textContent = `${selectedWarehouses.size} 个仓库`;
        updateControls();
      });
    });
    updateControls();
  }

  async function loadWarehouses(generation = accountGeneration, id = accountId) {
    if (!id || !isCurrentAccount(generation, id) || starting || running || loadingWarehouses) return;
    loadingWarehouses = true;
    $("lzWarehouseStatus").textContent = "正在读取账号有权限的全部仓库…";
    updateControls();
    try {
      const result = await HQYL.api().get_lz_warehouse_inventory_warehouses({ account_id: id });
      if (!isCurrentAccount(generation, id)) return;
      if (!result.ok) throw new Error(result.error || "仓库列表加载失败");
      if (result.account_id && String(result.account_id) !== id) throw new Error("仓库列表与当前账号不一致，请刷新重试");
      warehouseNames = Array.from(new Set((Array.isArray(result.warehouses) ? result.warehouses : [])
        .map((item) => String(typeof item === "string" ? item : item?.name || "").trim()).filter(Boolean)));
      const previous = pendingWarehouseSelection || (warehousesLoaded ? selectedWarehouses : new Set(DEFAULT_WAREHOUSES));
      selectedWarehouses = new Set(warehouseNames.filter((name) => previous.has(name)));
      pendingWarehouseSelection = null;
      warehousesLoaded = true;
      $("warehouseCount").textContent = `${selectedWarehouses.size} 个仓库`;
      $("lzWarehouseStatus").textContent = warehouseNames.length
        ? `已加载 ${warehouseNames.length} 个有权限的仓库；每个勾选仓库独立生成一个 Excel。`
        : "该账号暂无有权限的仓库，请检查权限后刷新。";
      renderWarehouses();
    } catch (error) {
      if (!isCurrentAccount(generation, id)) return;
      const message = inventoryErrorMessage(error);
      if (message === BACKEND_UPDATE_HINT) {
        ready = false;
        $("lzAccountNote").textContent = message;
      }
      $("lzWarehouseStatus").textContent = message === BACKEND_UPDATE_HINT
        ? message : `仓库加载失败：${message}，可点击“刷新仓库”重试。`;
      HQYL.showToast(message);
    } finally {
      if (isCurrentAccount(generation, id)) {
        loadingWarehouses = false;
        updateControls();
      }
    }
  }

  async function prepareAccount(generation = accountGeneration, id = accountId) {
    if (!id || !isCurrentAccount(generation, id)) return null;
    ready = false;
    $("lzRunBtn").disabled = true;
    const info = await HQYL.api().get_lz_warehouse_inventory_info({ account_id: id });
    if (!isCurrentAccount(generation, id)) return null;
    if (!info.ok) {
      $("lzAccountNote").textContent = info.error || "当前账号加载失败，请检查账号设置。";
      throw new Error($("lzAccountNote").textContent);
    }
    if (String(info.account_id || "") !== id) throw new Error("返回账号与当前选择不一致，请刷新重试");
    $("lzAccountNote").textContent = `本次使用当前马帮账号：${info.account_name || HQYL.activeAccount("mabang")?.name || ""}；切换账号后会重新加载仓库。`;
    ready = true;
    updateControls();
    return info;
  }

  async function runQuery() {
    if (starting || running || loadingWarehouses) return;
    if (!selectedWarehouses.size) { HQYL.showToast("请至少选择一个仓库"); return; }
    const selectedOutputDir = $("lzOutputDir").value.trim();
    if (!selectedOutputDir) { HQYL.showToast("请选择输出目录"); return; }
    starting = true;
    const generation = accountGeneration;
    const id = accountId;
    const names = warehouseNames.filter((name) => selectedWarehouses.has(name));
    updateControls();
    try {
      const info = await prepareAccount(generation, id);
      if (!info || !isCurrentAccount(generation, id)) return;
      if (!await saveOutputDir()) return;
      if (!isCurrentAccount(generation, id)) return;
      outputDir = selectedOutputDir;
      await HQYL.startTask(() => HQYL.api().start_lz_warehouse_inventory({
        account_id: id,
        warehouse_names: names,
        output_dir: outputDir,
      }), () => isCurrentAccount(generation, id));
    } catch (error) {
      if (isCurrentAccount(generation, id)) {
        const message = inventoryErrorMessage(error);
        if (message === BACKEND_UPDATE_HINT) $("lzAccountNote").textContent = message;
        HQYL.showToast(message);
      }
    } finally {
      if (isCurrentAccount(generation, id)) {
        starting = false;
        page.setRunning(running);
      }
    }
  }

  async function openExcel(path) {
    if (!path) return;
    try {
      const result = await HQYL.api().open_path(path);
      if (!result.ok) HQYL.showToast(result.error || "打开 Excel 失败");
    } catch (error) { HQYL.showToast(error.message || String(error)); }
  }

  function renderPreview() {
    const item = previewExports[Number($("lzPreviewWarehouse").value || 0)];
    $("lzPreviewPanel").hidden = !item;
    if (!item) { outputFile = ""; $("lzOpenFileBtn").disabled = true; return; }
    outputFile = item.output_file || "";
    const columns = Array.isArray(item.preview_columns) ? item.preview_columns : [];
    const rows = Array.isArray(item.rows) ? item.rows.slice(0, 100) : [];
    const count = Number(item.record_count || 0);
    $("lzOutputFile").textContent = outputFile || "未生成";
    $("lzPreviewNote").textContent = count > rows.length
      ? `${item.warehouse_name}：预览前 ${rows.length} 条，完整 ${count} 条见 Excel`
      : `${item.warehouse_name}：预览 ${rows.length} 条`;
    $("lzTableHead").innerHTML = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
    $("lzTableBody").innerHTML = rows.map((row) => `<tr>${columns.map((column) => `<td>${escapeHtml(row[column])}</td>`).join("")}</tr>`).join("");
    $("lzEmpty").hidden = rows.length > 0;
    $("lzOpenFileBtn").disabled = !outputFile;
  }

  const page = {
    key: "lz_warehouse_inventory",
    title: "lz各仓实时库存",
    taskContextFilter(context) { return !!accountId && String(context.account_id || "") === accountId && isCurrentAccount(accountGeneration, accountId); },
    onAccountsChanged() {
      if (initialized) switchAccount();
    },
    async init(info) {
      $("lzInventoryForm").addEventListener("submit", (event) => { event.preventDefault(); runQuery(); });
      $("lzWarehouseSearch").addEventListener("input", renderWarehouses);
      $("lzRefreshWarehousesBtn").addEventListener("click", () => loadWarehouses());
      $("lzBindAccountBtn").addEventListener("click", () => HQYL.openAccountDialog("mabang"));
      $("lzSelectWarehousesBtn").addEventListener("click", () => {
        visibleWarehouses().forEach((name) => selectedWarehouses.add(name));
        $("warehouseCount").textContent = `${selectedWarehouses.size} 个仓库`;
        renderWarehouses();
      });
      $("lzClearWarehousesBtn").addEventListener("click", () => {
        visibleWarehouses().forEach((name) => selectedWarehouses.delete(name));
        $("warehouseCount").textContent = `${selectedWarehouses.size} 个仓库`;
        renderWarehouses();
      });
      $("lzPreviewWarehouse").addEventListener("change", renderPreview);
      $("lzChooseDirBtn").addEventListener("click", async () => {
        const revision = outputRevision;
        try {
          const result = await HQYL.api().choose_scoped_output_dir({ tool: "lz_warehouse_inventory", kind: "output", current_dir: $("lzOutputDir").value.trim() || outputDir });
          if (result.ok) {
            if (revision !== outputRevision) { await saveOutputDir(true); return; }
            outputDir = result.path; $("lzOutputDir").value = outputDir;
            outputRevision += 1;
            await saveOutputDir();
          }
          else if (!result.cancelled) HQYL.showToast(result.error || "选择目录失败");
        } catch (error) { HQYL.showToast(error.message || String(error)); }
      });
      $("lzOutputDir").addEventListener("input", () => { outputRevision += 1; });
      $("lzOutputDir").addEventListener("change", () => saveOutputDir());
      $("lzOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || resultOutputDir || outputDir));
      $("lzOpenFileBtn").addEventListener("click", () => openExcel(outputFile));
      initialized = true;
      await switchAccount(true);
    },
    setRunning(value) {
      running = value;
      updateControls();
    },
    resetResult() {
      outputFile = "";
      resultOutputDir = "";
      previewExports = [];
      $("recordCount").textContent = "0";
      $("sourceCount").textContent = "查询结果 0 条";
      $("exportPageCount").textContent = "0";
      $("warehouseCount").textContent = `${selectedWarehouses.size} 个仓库`;
      $("lzFileStatus").textContent = "未生成";
      $("lzResult").hidden = true;
      $("lzOpenFileBtn").disabled = true;
      $("lzOpenOutputBtn").disabled = true;
      $("lzExportFilesBody").innerHTML = "";
      $("lzTableHead").innerHTML = "";
      $("lzTableBody").innerHTML = "";
      $("lzPreviewWarehouse").innerHTML = "";
      $("lzFailureSummary").innerHTML = "";
    },
    onTaskRestored(task) {
      const names = task?.context?.warehouse_names;
      if (Array.isArray(names)) {
        pendingWarehouseSelection = new Set(names);
        if (warehousesLoaded) {
          selectedWarehouses = new Set(warehouseNames.filter((name) => pendingWarehouseSelection.has(name)));
          pendingWarehouseSelection = null;
          renderWarehouses();
        }
      }
    },
    applyResult(result) {
      resultOutputDir = result.output_dir || result.output_root || "";
      const count = Number(result.record_count || 0);
      const sourceCount = Number(result.source_record_count ?? count);
      const pages = Number(result.export_page_count || 0);
      const warehouses = Object.entries(result.warehouse_counts || {});
      const exports = Array.isArray(result.exports) ? result.exports : [];
      const failures = Array.isArray(result.failures) ? result.failures : [];
      const legacy = !exports.length && !!result.output_file && !Array.isArray(result.output_files);
      previewExports = exports.length ? exports : legacy ? [{ ...result, warehouse_name: "历史合并文件" }] : [];
      $("recordCount").textContent = String(count);
      $("sourceCount").textContent = `查询结果 ${sourceCount} 条`;
      $("exportPageCount").textContent = String(pages);
      $("warehouseCount").textContent = `${result.selected_warehouse_count ?? result.warehouse_count ?? warehouses.length} 个仓库`;
      const generatedCount = previewExports.filter((item) => item.output_file).length;
      $("lzFileStatus").textContent = generatedCount ? `${generatedCount} 个文件${failures.length ? `，${failures.length} 仓失败` : ""}` : "未生成";
      $("lzResultSummary").textContent = legacy
        ? `历史结果：${count} 条记录保存在同一个合并文件；重新运行将按仓库独立导出。`
        : result.is_complete === false
          ? `${result.completion_message || "部分仓库导出失败"}；已生成 ${generatedCount} 个文件，共 ${count} 条记录、${pages} 个导出分页。`
          : `已生成 ${generatedCount} 个仓库文件，共 ${count} 条库存记录、${pages} 个导出分页。`;
      $("lzWarehouseSummary").innerHTML = warehouses.map(([name, value]) => `<span>${escapeHtml(name)}：${escapeHtml(value)} 条</span>`).join("");
      $("lzFailureSummary").hidden = !failures.length;
      $("lzFailureSummary").innerHTML = failures.map((item) => `<span>${escapeHtml(item.warehouse_name)}：${escapeHtml(item.error || "导出失败")}</span>`).join("");
      $("lzExportFiles").hidden = !previewExports.length;
      $("lzExportFilesBody").innerHTML = previewExports.map((item, index) => `<tr><td>${escapeHtml(item.warehouse_name)}</td><td>${escapeHtml(item.record_count ?? 0)}</td><td>${escapeHtml(item.export_page_count ?? 0)}</td><td>${escapeHtml(item.output_file)}</td><td><button class="secondary compact-btn" type="button" data-lz-open-file="${index}"${item.output_file ? "" : " disabled"}>打开 Excel</button></td></tr>`).join("");
      $("lzExportFilesBody").querySelectorAll("[data-lz-open-file]").forEach((button) => {
        button.addEventListener("click", () => openExcel(previewExports[Number(button.dataset.lzOpenFile)]?.output_file));
      });
      $("lzPreviewWarehouse").innerHTML = previewExports.map((item, index) => `<option value="${index}">${escapeHtml(item.warehouse_name)}（${escapeHtml(item.record_count ?? 0)} 条）</option>`).join("");
      $("lzPreviewNote").textContent = generatedCount ? "每仓前 100 条预览" : "暂无生成文件";
      renderPreview();
      $("lzResult").hidden = false;
      $("lzOpenOutputBtn").disabled = !generatedCount;
    },
  };
  HQYL.boot(page);
})();
