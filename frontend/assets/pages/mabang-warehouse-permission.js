(() => {
  const $ = HQYL.$;
  let warehouses = [];
  let selectedWarehouseIds = new Set();
  let previewToken = "";
  let previewRows = [];
  let pendingTaskMode = "";
  let running = false;
  let loadingWarehouses = false;
  let initialized = false;
  let accountId = "";
  const permissionLabels = { product: "查看商品", order: "查看订单", warehouse: "查看仓库" };

  function employeeNames() {
    const result = [];
    const seen = new Set();
    String($("employeeText").value || "").split(/\r?\n/).forEach((item) => {
      const name = item.trim().replace(/\s+/g, " ");
      const key = name.toLocaleLowerCase();
      if (!name || seen.has(key)) return;
      seen.add(key);
      result.push(name);
    });
    return result;
  }

  function selectedIds() {
    return warehouses.map((item) => item.id).filter((id) => selectedWarehouseIds.has(id));
  }

  function selectedPermissionTypes() {
    return [...document.querySelectorAll('input[name="permissionType"]:checked')].map((input) => input.value);
  }

  function filteredWarehouses() {
    const keyword = $("warehouseSearch").value.trim().toLocaleLowerCase();
    if (!keyword) return warehouses;
    return warehouses.filter((item) => item.name.toLocaleLowerCase().includes(keyword));
  }

  function updateCounts() {
    $("employeeCount").textContent = String(employeeNames().length);
    $("warehouseCount").textContent = String(selectedWarehouseIds.size);
    const visibleCount = filteredWarehouses().length;
    const permissionTypes = selectedPermissionTypes();
    $("permissionTypeCount").textContent = `${permissionTypes.length} 项`;
    $("permissionTypeSummary").textContent = permissionTypes.length
      ? permissionTypes.map((item) => permissionLabels[item]).join("、")
      : "尚未选择开通权限";
    if (!warehouses.length) {
      $("warehouseSelectionHint").textContent = "尚未加载仓库";
    } else {
      $("warehouseSelectionHint").textContent = `共 ${warehouses.length} 个仓库，当前显示 ${visibleCount} 个，已选择 ${selectedWarehouseIds.size} 个`;
    }
  }

  function setElementText(element, text, title = "") {
    element.textContent = text;
    if (title) element.title = title;
  }

  function renderWarehouseList() {
    const list = $("warehouseList");
    list.replaceChildren();
    const visible = filteredWarehouses();
    if (!warehouses.length || !visible.length) {
      const empty = document.createElement("div");
      empty.id = "warehouseEmpty";
      empty.className = "warehouse-empty";
      empty.textContent = warehouses.length ? "没有匹配的仓库" : "点击“加载仓库”读取马帮仓库列表";
      list.appendChild(empty);
      updateCounts();
      return;
    }
    visible.forEach((warehouse) => {
      const label = document.createElement("label");
      label.className = "warehouse-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = warehouse.id;
      input.checked = selectedWarehouseIds.has(warehouse.id);
      input.disabled = running;
      input.addEventListener("change", () => {
        if (input.checked) selectedWarehouseIds.add(warehouse.id);
        else selectedWarehouseIds.delete(warehouse.id);
        invalidatePreview(true);
        updateCounts();
      });
      const name = document.createElement("span");
      setElementText(name, warehouse.name, warehouse.name);
      label.append(input, name);
      list.appendChild(label);
    });
    updateCounts();
  }

  function statusMeta(status) {
    return {
      ready: ["ready", "可开通"],
      unchanged: ["skipped", "无需新增"],
      all_warehouses: ["skipped", "全部仓库"],
      not_found: ["failed", "未找到"],
      ambiguous: ["attention", "同名待确认"],
      error: ["failed", "检查失败"],
      success: ["success", "开通成功"],
      skipped: ["skipped", "已跳过"],
      failed: ["failed", "执行失败"],
    }[status] || ["attention", status || "未知"];
  }

  function permissionSideText(item, side, mode) {
    if (!selectedPermissionTypes().includes(side)) return "未选择";
    if (mode === "batch") {
      if (item.status === "failed") return "-";
      const count = Number({
        product: item.product_added_count,
        order: item.order_added_count,
        warehouse: item.warehouse_added_count,
      }[side] || 0);
      if (side === "order" && item.order_mode_changed) {
        return count ? `已补 ${count} 个并切换` : "已切按仓库";
      }
      return count ? `已补 ${count} 个` : "原本完整";
    }
    const missing = {
      product: item.missing_product_warehouse_ids,
      order: item.missing_order_warehouse_ids,
      warehouse: item.missing_view_warehouse_ids,
    }[side] || [];
    const targetCount = Number(item.target_warehouse_count || 0);
    if (side === "order" && !missing.length && item.order_view_mode && item.order_view_mode !== "2") {
      return "待切按仓库";
    }
    if (!missing.length) return `已包含 ${targetCount} 个`;
    return `缺 ${missing.length} 个`;
  }

  function renderRows(rows, mode) {
    const body = $("permissionResultBody");
    body.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.className = "permission-empty-row";
      cell.colSpan = 8;
      cell.textContent = "暂无员工处理结果";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    rows.forEach((item) => {
      const row = document.createElement("tr");
      const inputName = document.createElement("td");
      setElementText(inputName, item.input_name || "-");

      const employee = document.createElement("td");
      const employeeName = item.employee_name || "-";
      if (item.status === "ambiguous" && Array.isArray(item.candidates)) {
        const candidateText = item.candidates.map((candidate) => `${candidate.name || "-"} / ${candidate.mobile || "无手机"} / ${candidate.department || "无部门"}`).join("；");
        setElementText(employee, `${item.candidates.length} 名同名员工`, candidateText);
      } else {
        setElementText(employee, item.employee_id ? `${employeeName} (${item.employee_id})` : employeeName);
      }

      const contact = document.createElement("td");
      setElementText(contact, [item.department, item.mobile].filter(Boolean).join(" / ") || "-");

      const current = document.createElement("td");
      const missingProductNames = item.missing_product_warehouse_names || [];
      setElementText(
        current,
        permissionSideText(item, "product", mode),
        selectedPermissionTypes().includes("product")
          ? (missingProductNames.length ? `缺少：${missingProductNames.join("、")}` : "目标仓库均已包含")
          : "本次不开通查看商品",
      );

      const order = document.createElement("td");
      const missingOrderNames = item.missing_order_warehouse_names || [];
      setElementText(
        order,
        permissionSideText(item, "order", mode),
        selectedPermissionTypes().includes("order")
          ? (missingOrderNames.length ? `缺少：${missingOrderNames.join("、")}` : "目标仓库均已包含")
          : "本次不开通查看订单",
      );

      const changed = document.createElement("td");
      const missingWarehouseNames = item.missing_view_warehouse_names || [];
      setElementText(
        changed,
        permissionSideText(item, "warehouse", mode),
        selectedPermissionTypes().includes("warehouse")
          ? (missingWarehouseNames.length ? `缺少：${missingWarehouseNames.join("、")}` : "目标仓库均已包含")
          : "本次不开通查看仓库",
      );

      const state = document.createElement("td");
      const badge = document.createElement("span");
      const [kind, label] = statusMeta(item.status);
      badge.className = `permission-state ${kind}`;
      badge.textContent = label;
      state.appendChild(badge);

      const message = document.createElement("td");
      message.className = "permission-message-cell";
      setElementText(message, item.message || "-", item.message || "");
      row.append(inputName, employee, contact, current, order, changed, state, message);
      body.appendChild(row);
    });
  }

  function invalidatePreview(clearRows = false) {
    previewToken = "";
    $("applyBtn").disabled = true;
    $("readyCount").textContent = "0";
    if (clearRows) {
      previewRows = [];
      renderRows([], "preview");
      $("resultSummary").textContent = "输入已变化，请重新预览";
      $("statusText").textContent = "请重新完成预览检查";
    }
  }

  async function loadWarehouses() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", loadWarehouses);
      return;
    }
    loadingWarehouses = true;
    $("loadWarehousesBtn").disabled = true;
    $("loadWarehousesBtn").textContent = "正在加载...";
    try {
      const result = await HQYL.api().get_mabang_warehouse_options({ account_id: account.id });
      if (!result.ok) throw new Error(result.error || "仓库加载失败");
      warehouses = Array.isArray(result.warehouses) ? result.warehouses.map((item) => ({ id: String(item.id), name: String(item.name || item.id) })) : [];
      selectedWarehouseIds = new Set([...selectedWarehouseIds].filter((id) => warehouses.some((item) => item.id === id)));
      $("warehouseSearch").disabled = !warehouses.length;
      $("selectFilteredBtn").disabled = !warehouses.length;
      $("clearWarehouseBtn").disabled = !warehouses.length;
      invalidatePreview(true);
      renderWarehouseList();
      HQYL.showToast(`已加载 ${warehouses.length} 个仓库`);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    } finally {
      loadingWarehouses = false;
      $("loadWarehousesBtn").disabled = running;
      $("loadWarehousesBtn").textContent = "加载仓库";
    }
  }

  function validateInputs() {
    const names = employeeNames();
    const warehouseIds = selectedIds();
    const permissionTypes = selectedPermissionTypes();
    if (!names.length) throw new Error("请至少输入一名员工");
    if (!warehouses.length) throw new Error("请先加载仓库");
    if (!warehouseIds.length) throw new Error("请至少选择一个仓库");
    if (!permissionTypes.length) throw new Error("请至少选择一项开通权限");
    return { names, warehouseIds, permissionTypes };
  }

  async function runPreview() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runPreview);
      return;
    }
    let input;
    try {
      input = validateInputs();
    } catch (error) {
      HQYL.showToast(error.message || String(error));
      return;
    }
    invalidatePreview(true);
    pendingTaskMode = "preview";
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备预览 ${input.names.length} 名员工、${input.warehouseIds.length} 个仓库；权限：${input.permissionTypes.map((item) => permissionLabels[item]).join("、")}`);
      return HQYL.api().start_mabang_warehouse_permission_preview({
        account_id: account.id,
        employee_names: input.names,
        warehouse_ids: input.warehouseIds,
        permission_types: input.permissionTypes,
      });
    });
    pendingTaskMode = "";
  }

  async function runBatch() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runBatch);
      return;
    }
    let input;
    try {
      input = validateInputs();
      if (!previewToken) throw new Error("请先完成预览检查");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
      return;
    }
    pendingTaskMode = "batch";
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备为 ${previewRows.filter((row) => row.can_apply).length} 名员工开通：${input.permissionTypes.map((item) => permissionLabels[item]).join("、")}`);
      return HQYL.api().start_mabang_warehouse_permission_batch({
        account_id: account.id,
        preview_token: previewToken,
        employee_names: input.names,
        warehouse_ids: input.warehouseIds,
        permission_types: input.permissionTypes,
      });
    });
    pendingTaskMode = "";
  }

  const page = {
    key: "mabang_warehouse_permission",
    taskKeys: ["mabang_warehouse_permission_preview", "mabang_warehouse_permission_batch"],
    title: "马帮仓库权限批量开通",
    async init() {
      initialized = true;
      accountId = HQYL.activeAccount("mabang")?.id || "";
      $("permissionForm").addEventListener("submit", (event) => { event.preventDefault(); runPreview(); });
      $("employeeText").addEventListener("input", () => { updateCounts(); invalidatePreview(true); });
      document.querySelectorAll('input[name="permissionType"]').forEach((input) => {
        input.addEventListener("change", () => { updateCounts(); invalidatePreview(true); });
      });
      $("warehouseSearch").addEventListener("input", renderWarehouseList);
      $("loadWarehousesBtn").addEventListener("click", loadWarehouses);
      $("selectFilteredBtn").addEventListener("click", () => {
        filteredWarehouses().forEach((item) => selectedWarehouseIds.add(item.id));
        invalidatePreview(true);
        renderWarehouseList();
      });
      $("clearWarehouseBtn").addEventListener("click", () => {
        selectedWarehouseIds.clear();
        invalidatePreview(true);
        renderWarehouseList();
      });
      $("applyBtn").addEventListener("click", runBatch);
      updateCounts();
    },
    onAccountsChanged() {
      if (!initialized) return;
      const nextAccountId = HQYL.activeAccount("mabang")?.id || "";
      if (nextAccountId === accountId) return;
      accountId = nextAccountId;
      warehouses = [];
      selectedWarehouseIds.clear();
      $("warehouseSearch").value = "";
      $("warehouseSearch").disabled = true;
      $("selectFilteredBtn").disabled = true;
      $("clearWarehouseBtn").disabled = true;
      invalidatePreview(true);
      renderWarehouseList();
    },
    taskContextFilter(context) {
      const active = HQYL.activeAccount("mabang");
      return !context?.account_id || context.account_id === active?.id;
    },
    setRunning(value) {
      const stateChanged = running !== value;
      running = value;
      $("employeeText").disabled = value;
      document.querySelectorAll('input[name="permissionType"]').forEach((input) => { input.disabled = value; });
      $("warehouseSearch").disabled = value || !warehouses.length;
      $("loadWarehousesBtn").disabled = value || loadingWarehouses;
      $("previewBtn").disabled = value;
      $("selectFilteredBtn").disabled = value || !warehouses.length;
      $("clearWarehouseBtn").disabled = value || !warehouses.length;
      $("applyBtn").disabled = value || !previewToken || !previewRows.some((row) => row.can_apply);
      if (stateChanged) renderWarehouseList();
    },
    resetResult() {
      if (pendingTaskMode === "preview") {
        previewToken = "";
        previewRows = [];
        $("readyCount").textContent = "0";
        $("resultSummary").textContent = "正在检查";
        renderRows([], "preview");
      }
    },
    applyResult(result) {
      if (result.mode === "preview") {
        previewToken = result.preview_token || "";
        previewRows = Array.isArray(result.rows) ? result.rows : [];
        renderRows(previewRows, "preview");
        $("readyCount").textContent = String(result.ready_count || 0);
        $("resultSummary").textContent = `可开通 ${result.ready_count || 0}，无需新增 ${result.skipped_count || 0}，待处理 ${result.attention_count || 0}`;
        $("statusText").textContent = result.ready_count ? "预览完成，可以开始开通" : "预览完成，没有可开通员工";
        $("applyBtn").disabled = running || !previewToken || !previewRows.some((row) => row.can_apply);
        return;
      }
      if (result.mode === "batch") {
        previewToken = "";
        previewRows = [];
        renderRows(result.rows || [], "batch");
        $("readyCount").textContent = String(result.success_count || 0);
        $("resultSummary").textContent = `成功 ${result.success_count || 0}，跳过 ${result.skipped_count || 0}，失败 ${result.failed_count || 0}`;
        $("statusText").textContent = result.failed_count ? "执行完成，部分员工需要处理" : "批量开通完成";
        $("applyBtn").disabled = true;
      }
    },
  };

  HQYL.boot(page);
})();
