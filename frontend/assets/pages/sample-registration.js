(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let configFile = null;
  let groups = [];
  let selectedGroupIndex = -1;
  let pendingUnmatchedSkip = false;
  let dingtalkUsers = [];

  // ── 日期模式切换 ──
  function updateDateFields() {
    const mode = $("dateMode").value;
    $("singleDateField").hidden = mode !== "single";
    $("startDateField").hidden = mode !== "range";
    $("endDateField").hidden = mode !== "range";
  }

  function resolveDates() {
    const mode = $("dateMode").value;
    const today = new Date();
    const fmt = (d) => d.toISOString().slice(0, 10);
    if (mode === "yesterday") {
      const y = new Date(today); y.setDate(y.getDate() - 1);
      return { start_date: fmt(y), end_date: fmt(y) };
    }
    if (mode === "single") {
      const d = $("singleDate").value;
      return { start_date: d, end_date: d };
    }
    return { start_date: $("startDate").value, end_date: $("endDate").value };
  }

  // ── 概览计数 ──
  function updateOverviewCounts() {
    $("groupCount").textContent = String(groups.length);
    const total = groups.reduce((sum, g) => sum + (g.stores || []).filter((s) => s.trim()).length, 0);
    $("storeCount").textContent = String(total);
  }

  // ── 组长列表渲染 ──
  function renderGroupList() {
    const list = $("groupList");
    list.textContent = "";
    groups.forEach((g, i) => {
      const item = document.createElement("div");
      item.className = "config-group-item" + (i === selectedGroupIndex ? " active" : "");
      const storeCount = (g.stores || []).filter((s) => s.trim()).length;
      item.innerHTML = `<strong>${g.leader || "(未命名)"}</strong><small>${storeCount} 个店铺</small>`;
      item.addEventListener("click", () => selectGroup(i));
      list.appendChild(item);
    });
    updateOverviewCounts();
  }

  function selectGroup(index) {
    syncCurrentGroup();
    selectedGroupIndex = index;
    renderGroupList();
    renderGroupDetail();
  }

  function renderGroupDetail() {
    const empty = $("detailEmpty");
    const form = $("detailForm");
    if (selectedGroupIndex < 0 || selectedGroupIndex >= groups.length) {
      empty.hidden = false;
      form.hidden = true;
      $("deleteGroupBtn").disabled = true;
      return;
    }
    empty.hidden = true;
    form.hidden = false;
    $("deleteGroupBtn").disabled = false;
    const g = groups[selectedGroupIndex];
    $("detailLeader").value = g.leader || "";
    $("detailTargetSheet").value = g.target_sheet || "";
    $("detailOutputFileName").value = g.output_file_name || "";
    $("detailStores").value = (g.stores || []).join("\n");
    updateDetailStoreCount();
  }

  function syncCurrentGroup() {
    if (selectedGroupIndex < 0 || selectedGroupIndex >= groups.length) return;
    const g = groups[selectedGroupIndex];
    g.leader = $("detailLeader").value.trim();
    g.target_sheet = $("detailTargetSheet").value.trim();
    g.output_file_name = $("detailOutputFileName").value.trim();
    g.stores = $("detailStores").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    renderGroupList();
  }

  function updateDetailStoreCount() {
    const count = $("detailStores").value.split(/\r?\n/).filter((s) => s.trim()).length;
    $("detailStoreCount").textContent = `${count} 个店铺`;
  }

  // ── Tab 切换 ──
  function switchTab(tab) {
    syncCurrentGroup();
    const isRun = tab === "run";
    $("tabRun").classList.toggle("active", isRun);
    $("tabConfig").classList.toggle("active", !isRun);
    $("runPanel").hidden = !isRun;
    $("configPanel").hidden = isRun;
  }

  function updateDingTalkFields() {
    const enabled = $("enableTargetUpdate").checked;
    ["targetDocId", "dingtalkOperatorName"].forEach((id) => {
      $(id).disabled = !enabled;
    });
    const syncSection = $("syncSection");
    if (syncSection) {
      syncSection.classList.toggle("is-disabled", !enabled);
    }
  }

  function renderDingTalkOperatorList() {
    const list = $("dingtalkOperatorList");
    if (!list) return;
    list.textContent = "";
    dingtalkUsers.forEach((user) => {
      if (!user || !user.name) return;
      const option = document.createElement("option");
      option.value = user.name;
      list.appendChild(option);
    });
  }

  // ── 配置收集 ──
  function collectConfig() {
    syncCurrentGroup();
    return {
      target_doc_id: $("targetDocId").value.trim(),
      enable_target_update: $("enableTargetUpdate").checked,
      dingtalk_operator_name: $("dingtalkOperatorName").value.trim(),
      output_dir: $("outputDir").value.trim(),
      groups: groups.map((g) => ({
        leader: g.leader,
        target_sheet: g.target_sheet,
        output_file_name: g.output_file_name,
        stores: g.stores,
      })),
    };
  }

  // ── 加载/保存配置 ──
  async function loadConfig() {
    try {
      const result = await HQYL.api().get_sample_registration_config();
      if (!result.ok) throw new Error(result.error || "读取配置失败");
      const cfg = result.config || {};
      $("targetDocId").value = cfg.target_doc_id || "";
      $("enableTargetUpdate").checked = cfg.enable_target_update === true;
      $("dingtalkOperatorName").value = cfg.dingtalk_operator_name || "";
      $("outputDir").value = cfg.output_dir || "";
      updateDingTalkFields();
      groups = (cfg.groups || []).map((g) => ({
        leader: g.leader || "",
        target_sheet: g.target_sheet || "",
        output_file_name: g.output_file_name || "",
        stores: Array.isArray(g.stores) ? g.stores : [],
      }));
      selectedGroupIndex = groups.length > 0 ? 0 : -1;
      renderGroupList();
      renderGroupDetail();
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function saveConfig() {
    syncCurrentGroup();
    try {
      const result = await HQYL.api().save_sample_registration_config(collectConfig());
      if (!result.ok) throw new Error(result.error || "保存配置失败");
      HQYL.showToast("配置已保存");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  // ── 匹配校验 ──
  async function runMatchCheck() {
    syncCurrentGroup();
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runMatchCheck);
      return;
    }
    const cfg = collectConfig();
    if (!cfg.groups.length) {
      HQYL.showToast("请先配置至少一个组长");
      switchTab("config");
      return;
    }
    $("matchBtn").disabled = true;
    $("matchBtn").textContent = "匹配中...";
    HQYL.appendLog("正在执行店铺匹配校验...");
    try {
      const result = await HQYL.api().start_sample_store_match({
        account_id: account.id,
        config: cfg,
      });
      if (!result.ok) {
        throw new Error(result.error || "匹配任务启动失败");
      }
      // 等待任务完成
      const matchResult = await waitForTask(result.id);
      if (!matchResult) throw new Error("匹配任务超时");
      if (matchResult.status === "failed") throw new Error(matchResult.error || "匹配失败");

      const data = matchResult.result || {};
      HQYL.appendLog(`匹配完成：成功 ${data.resolved_count} 个，失败 ${data.unresolved_count} 个`);
      if (data.unresolved_count > 0 && data.unresolved_rows && data.unresolved_rows.length > 0) {
        showUnmatchedModal(data.unresolved_rows);
      } else {
        HQYL.showToast(`全部 ${data.resolved_count} 个店铺匹配成功`);
      }
    } catch (error) {
      HQYL.appendLog(`匹配失败：${error.message}`);
      HQYL.showToast(error.message || String(error));
    } finally {
      $("matchBtn").disabled = false;
      $("matchBtn").textContent = "匹配校验";
    }
  }

  function waitForTask(taskId) {
    return new Promise((resolve) => {
      let elapsed = 0;
      const timer = setInterval(async () => {
        elapsed += 1000;
        if (elapsed > 300000) { clearInterval(timer); resolve(null); return; }
        try {
          const task = await HQYL.api().get_task_status(taskId);
          if (!task.ok) return;
          if (task.logs) HQYL.appendLog(task.logs.slice(-1).join(""));
          if (task.status === "success" || task.status === "failed") {
            clearInterval(timer);
            resolve(task);
          }
        } catch { /* retry */ }
      }, 1000);
    });
  }

  // ── 未匹配弹窗 ──
  function showUnmatchedModal(rows) {
    const tbody = $("unmatchedTable").querySelector("tbody");
    tbody.textContent = "";
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${row["组长"] || ""}</td><td>${row["用户填写店铺"] || ""}</td><td>${row["原因"] || ""}</td><td>${row["候选马帮店铺"] || ""}</td>`;
      tbody.appendChild(tr);
    });
    $("unmatchedModal").classList.remove("hidden");
    pendingUnmatchedSkip = false;
  }

  function closeUnmatchedModal() {
    $("unmatchedModal").classList.add("hidden");
  }

  // ── 正式运行 ──
  async function runRegistration(skipUnmatched) {
    syncCurrentGroup();
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", () => runRegistration(skipUnmatched));
      return;
    }
    const cfg = collectConfig();
    if (!cfg.groups.length) {
      HQYL.showToast("请先配置至少一个组长");
      switchTab("config");
      return;
    }
    const dates = resolveDates();
    if (!dates.start_date) {
      HQYL.showToast("请选择日期");
      return;
    }

    await HQYL.startTask(() => {
      HQYL.appendLog("正在启动网红寄样登记...");
      return HQYL.api().start_sample_registration({
        account_id: account.id,
        start_date: dates.start_date,
        end_date: dates.end_date,
        output_dir: $("outputDir").value.trim(),
        allow_unmatched: !!skipUnmatched,
        config: cfg,
      });
    });
  }

  function handleSubmit(event) {
    event.preventDefault();
    runRegistration(false);
  }

  // ── 页面初始化 ──
  const page = {
    key: "sample_registration",
    taskKey: "sample_registration",
    title: "网红寄样登记",
    async init(info) {
      outputDir = (info.settings || {}).output_dir || "";
      dingtalkUsers = ((((info.settings || {}).dingtalk || {}).operator_names || [])).map((name) => ({ name }));
      renderDingTalkOperatorList();
      $("outputDir").value = outputDir;
      // 设置默认日期
      const today = new Date();
      const yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1);
      const fmt = (d) => d.toISOString().slice(0, 10);
      $("singleDate").value = fmt(yesterday);
      $("startDate").value = fmt(yesterday);
      $("endDate").value = fmt(yesterday);

      await loadConfig();
      updateDateFields();

      // 事件绑定
      $("dateMode").addEventListener("change", updateDateFields);
      $("enableTargetUpdate").addEventListener("change", updateDingTalkFields);
      $("runForm").addEventListener("submit", handleSubmit);
      $("matchBtn").addEventListener("click", runMatchCheck);
      $("saveConfigBtn").addEventListener("click", saveConfig);
      $("chooseDirBtn").addEventListener("click", async () => {
        const r = await HQYL.api().choose_output_dir();
        if (r.ok) { $("outputDir").value = r.path; outputDir = r.path; }
      });
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputDir));
      $("copyLogBtn").addEventListener("click", () => {
        navigator.clipboard.writeText($("logBox").textContent).then(() => HQYL.showToast("日志已复制"));
      });
      $("tabRun").addEventListener("click", () => switchTab("run"));
      $("tabConfig").addEventListener("click", () => switchTab("config"));
      $("addGroupBtn").addEventListener("click", () => {
        syncCurrentGroup();
        groups.push({ leader: "", target_sheet: "", output_file_name: "", stores: [] });
        selectedGroupIndex = groups.length - 1;
        renderGroupList();
        renderGroupDetail();
        $("detailLeader").focus();
      });
      $("deleteGroupBtn").addEventListener("click", () => {
        if (selectedGroupIndex < 0) return;
        groups.splice(selectedGroupIndex, 1);
        selectedGroupIndex = Math.min(selectedGroupIndex, groups.length - 1);
        renderGroupList();
        renderGroupDetail();
      });
      $("detailStores").addEventListener("input", updateDetailStoreCount);
      $("unmatchedCancelBtn").addEventListener("click", closeUnmatchedModal);
      $("unmatchedSkipBtn").addEventListener("click", () => {
        closeUnmatchedModal();
        runRegistration(true);
      });
      $("closeUnmatchedBtn").addEventListener("click", closeUnmatchedModal);
      $("unmatchedModal").addEventListener("click", (e) => { if (e.target === $("unmatchedModal")) closeUnmatchedModal(); });
      updateOverviewCounts();
    },
    setRunning(running) {
      $("runBtn").disabled = running;
      $("matchBtn").disabled = running;
      $("saveConfigBtn").disabled = running;
    },
    resetResult() {
      $("outputFile").textContent = "0";
      $("statusText").textContent = "任务准备中";
      $("openOutputBtn").disabled = true;
    },
    applyResult(result) {
      $("outputFile").textContent = String(result.processed_count ?? 0);
      $("statusText").textContent = `成功 ${result.processed_count ?? 0} 条，在线追加 ${result.online_append_count ?? 0} 条`;
      if (result.output_directory) outputDir = result.output_directory;
      $("openOutputBtn").disabled = !outputDir;
    },
  };

  HQYL.boot(page);
})();
