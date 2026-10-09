(() => {
  const $ = HQYL.$;
  let previewToken = "";
  let previewRows = [];
  let pendingTaskMode = "";
  let running = false;
  let initialized = false;
  let accountId = "";
  let taskSignature = "";
  let observedTaskId = "";
  let inputRevision = 0;
  let taskInputRevision = 0;
  let inputTouched = false;
  const productViewLabels = { 1: "按商品父目录查看", 2: "按仓库查看", 3: "按人员岗位查看", 4: "查看所有商品" };

  function normalizeNames(values) {
    const result = [];
    const seen = new Set();
    values.forEach((value) => {
      const name = String(value || "").trim().replace(/\s+/g, " ");
      const key = name.toLocaleLowerCase();
      if (!name || seen.has(key)) return;
      seen.add(key);
      result.push(name);
    });
    return result;
  }

  function employeeNames() {
    return normalizeNames(String($("employeeText").value || "").split(/\r?\n/));
  }

  function inputSignature(names = employeeNames(), id = HQYL.activeAccount("mabang")?.id || "") {
    return JSON.stringify([String(id), names]);
  }

  function resultMatchesInput() {
    return taskInputRevision === inputRevision && taskSignature === inputSignature();
  }

  function updateControls() {
    $("employeeCount").textContent = String(employeeNames().length);
    $("employeeText").disabled = running;
    $("previewBtn").disabled = running;
    $("applyBtn").disabled = running || !previewToken || !resultMatchesInput() || !previewRows.some((row) => row.status === "ready" && row.can_apply);
  }

  function statusMeta(status) {
    return {
      ready: ["ready", "可保存"],
      unchanged: ["skipped", "无需修改"],
      not_found: ["failed", "未找到"],
      ambiguous: ["attention", "同名待确认"],
      inactive: ["attention", "已停用/状态异常"],
      error: ["failed", "检查失败"],
      success: ["success", "保存成功"],
      skipped: ["skipped", "已跳过"],
      failed: ["failed", "保存失败"],
    }[status] || ["attention", status || "未知"];
  }

  function operationSummary(item) {
    if (item.status !== "ready" || !item.can_apply) return item.message || "-";
    const changes = [];
    if (!item.has_developer) changes.push("保留原岗位并勾选开发员");
    if (String(item.product_view_mode) !== "1") changes.push("查看商品 → 库存 SKU → 按商品父目录查看");
    if (!changes.length) changes.push("核对开发员及商品查看设置后保存");
    changes.push("保留已选目录、仓库及其他权限");
    return changes.join("；");
  }

  function textCell(value) {
    const cell = document.createElement("td");
    cell.textContent = value || "-";
    cell.title = cell.textContent;
    return cell;
  }

  function renderRows(rows) {
    const body = $("permissionResultBody");
    body.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      const row = document.createElement("tr");
      const cell = textCell("暂无员工处理结果");
      cell.className = "permission-empty-row";
      cell.colSpan = 7;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    rows.forEach((item) => {
      const row = document.createElement("tr");
      const employee = textCell(item.employee_id ? `${item.employee_name || "-"} (${item.employee_id})` : item.employee_name);
      if (item.status === "ambiguous" && Array.isArray(item.candidates)) {
        employee.textContent = `${item.candidates.length} 名同名员工`;
        employee.title = item.candidates.map((candidate) => [candidate.name || candidate.employee_name, candidate.mobile, candidate.department].filter(Boolean).join(" / ")).join("；");
      }
      const stations = Array.isArray(item.current_station_names) ? [...item.current_station_names] : [];
      if (item.has_developer && !stations.includes("开发员")) stations.push("开发员");
      const state = document.createElement("td");
      const badge = document.createElement("span");
      const [kind, label] = statusMeta(item.status);
      badge.className = `permission-state ${kind}`;
      badge.textContent = label;
      state.appendChild(badge);
      const message = textCell(operationSummary(item));
      message.className = "permission-message-cell";
      row.append(
        textCell(item.input_name), employee,
        textCell([item.department, item.mobile].filter(Boolean).join(" / ")),
        textCell(stations.join("、") || (item.employee_id ? "未设置岗位" : "-")),
        textCell(productViewLabels[item.product_view_mode] || (item.employee_id ? "未识别" : "-")),
        state, message,
      );
      body.appendChild(row);
    });
  }

  function invalidatePreview() {
    inputRevision += 1;
    previewToken = "";
    previewRows = [];
    $("readyCountLabel").textContent = "可保存员工";
    $("readyCount").textContent = "0";
    $("attentionCount").textContent = "0";
    $("resultSummary").textContent = "输入已变化，请重新预览";
    $("statusText").textContent = "请重新完成预览检查";
    renderRows([]);
    updateControls();
  }

  function validatedNames() {
    const names = employeeNames();
    if (!names.length) throw new Error("请至少输入一名员工");
    if (names.length > 100) throw new Error("每批最多处理 100 名员工，请分批操作");
    return names;
  }

  async function runPreview() {
    if (running) return;
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runPreview);
      return;
    }
    let names;
    try { names = validatedNames(); }
    catch (error) { HQYL.showToast(error.message || String(error)); return; }
    invalidatePreview();
    taskSignature = inputSignature(names, account.id);
    taskInputRevision = inputRevision;
    pendingTaskMode = "preview";
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备检查 ${names.length} 名员工的开发员岗位和商品查看设置`);
      return HQYL.api().start_mabang_developer_permission_preview({ account_id: account.id, employee_names: names });
    });
    pendingTaskMode = "";
  }

  async function runBatch() {
    if (running) return;
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runBatch);
      return;
    }
    let names;
    try {
      names = validatedNames();
      if (!previewToken || !resultMatchesInput()) throw new Error("名单或账号已变化，请重新完成预览检查");
      if (!previewRows.some((row) => row.status === "ready" && row.can_apply)) throw new Error("没有可保存的员工");
    } catch (error) { HQYL.showToast(error.message || String(error)); return; }
    const token = previewToken;
    const readyCount = previewRows.filter((row) => row.status === "ready" && row.can_apply).length;
    previewToken = "";
    pendingTaskMode = "batch";
    updateControls();
    const task = await HQYL.startTask(() => {
      HQYL.appendLog(`准备为 ${readyCount} 名员工添加开发员并保存商品查看设置`);
      return HQYL.api().start_mabang_developer_permission_batch({ account_id: account.id, employee_names: names, preview_token: token });
    });
    pendingTaskMode = "";
    if (!task && resultMatchesInput()) {
      previewToken = token;
      updateControls();
    }
  }

  const page = {
    key: "mabang_developer_permission",
    taskKeys: ["mabang_developer_permission_preview", "mabang_developer_permission_batch"],
    title: "马帮批量添加开发员",
    async init() {
      initialized = true;
      accountId = HQYL.activeAccount("mabang")?.id || "";
      $("permissionForm").addEventListener("submit", (event) => { event.preventDefault(); runPreview(); });
      $("employeeText").addEventListener("input", () => { inputTouched = true; invalidatePreview(); });
      $("applyBtn").addEventListener("click", runBatch);
      updateControls();
    },
    onAccountsChanged() {
      if (!initialized) return;
      const nextAccountId = HQYL.activeAccount("mabang")?.id || "";
      if (nextAccountId === accountId) return;
      accountId = nextAccountId;
      inputTouched = true;
      invalidatePreview();
    },
    taskContextFilter(context) {
      return Boolean(context?.account_id) && String(context.account_id) === String(HQYL.activeAccount("mabang")?.id || "");
    },
    onTaskUpdate(task) {
      if (task.id === observedTaskId) return;
      observedTaskId = task.id;
      if (pendingTaskMode) return;
      const context = task.context || {};
      const names = normalizeNames(Array.isArray(context.employee_names) ? context.employee_names : (task.result?.rows || []).map((row) => row.input_name));
      if (!inputTouched && !employeeNames().length && names.length) $("employeeText").value = names.join("\n");
      taskSignature = inputSignature(names, context.account_id || "");
      taskInputRevision = inputRevision;
      updateControls();
    },
    setRunning(value) {
      running = value;
      updateControls();
    },
    resetResult() {
      if (pendingTaskMode !== "preview") return;
      previewToken = "";
      previewRows = [];
      $("readyCountLabel").textContent = "可保存员工";
      $("readyCount").textContent = "0";
      $("attentionCount").textContent = "0";
      $("resultSummary").textContent = "正在检查";
      renderRows([]);
    },
    applyResult(result) {
      if (!resultMatchesInput()) {
        previewToken = "";
        previewRows = [];
        $("readyCount").textContent = "0";
        $("resultSummary").textContent = "任务结果对应其他输入，请重新预览";
        $("statusText").textContent = "名单或账号已变化";
        updateControls();
        return;
      }
      if (result.mode === "preview") {
        previewToken = result.preview_token || "";
        previewRows = Array.isArray(result.rows) ? result.rows : [];
        renderRows(previewRows);
        $("readyCountLabel").textContent = "可保存员工";
        $("readyCount").textContent = String(result.ready_count || 0);
        $("attentionCount").textContent = String(result.attention_count || 0);
        $("resultSummary").textContent = `可保存 ${result.ready_count || 0}，无需修改 ${result.skipped_count || 0}，待处理 ${result.attention_count || 0}`;
        $("statusText").textContent = result.ready_count ? "预览完成，可以批量保存" : "预览完成，没有可保存员工";
      } else if (result.mode === "batch") {
        previewToken = "";
        previewRows = [];
        renderRows(result.rows || []);
        $("readyCountLabel").textContent = "已保存员工";
        $("readyCount").textContent = String(result.success_count || 0);
        $("attentionCount").textContent = String(result.failed_count || 0);
        $("resultSummary").textContent = `成功 ${result.success_count || 0}，跳过 ${result.skipped_count || 0}，失败 ${result.failed_count || 0}`;
        $("statusText").textContent = result.failed_count ? "保存完成，部分员工需要处理" : "批量保存完成";
      }
      updateControls();
    },
  };

  HQYL.boot(page);
})();
