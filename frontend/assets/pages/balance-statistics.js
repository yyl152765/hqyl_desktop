(() => {
  const $ = HQYL.$;
  const platform = document.body.dataset.balancePlatform;
  const isTemu = platform === "temu";
  const title = `${isTemu ? "TEMU" : "Lazada"} 余额统计`;
  const fields = isTemu ? ["total", "pending"] : ["income", "balance", "ads", "processing"];
  const columnCount = fields.length + 6;
  const countries = { PH: "菲律宾", MY: "马来西亚", TH: "泰国", ID: "印度尼西亚", VN: "越南", SG: "新加坡", GLOBAL: "全球" };
  const statusNames = { success: "成功", partial: "部分完成", partial_success: "部分完成", failed: "失败", pending: "等待", running: "运行中", unmatched_store: "未匹配店铺", page_unavailable: "页面不可用", need_manual_review: "需人工复核" };
  let running = false, preparing = false, initialized = false;
  let snapshot = null, accountId = "";
  const activeId = () => HQYL.activeAccount("ziniao")?.id || "";
  const busy = () => running || preparing;
  const storeNames = () => [...new Set($("storeNames").value.split(/\r?\n/).map(name => name.trim()).filter(Boolean))];

  function previousMonth() {
    const date = new Date();
    date.setDate(1);
    date.setMonth(date.getMonth() - 1);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
  }

  function showIssue(message) {
    $("startIssues").textContent = message || "";
    $("startIssues").hidden = !message;
  }

  function controls() {
    ["storeNames", "balanceOutputDir", "balanceMonth", "balanceCountry", "chooseOutputBtn", "resetMonthBtn"].forEach(id => {
      if ($(id)) $(id).disabled = busy();
    });
    $("startBtn").disabled = busy() || !storeNames().length || !$("balanceOutputDir").value.trim() || (isTemu && !$("balanceMonth").value);
    $("retryBtn").disabled = busy() || !activeId() || !snapshot?.manifest_path || snapshot.is_complete !== false || snapshot.account_id !== activeId();
    $("openFileBtn").disabled = !snapshot?.output_file;
    $("openDirectoryBtn").disabled = !snapshot?.output_dir;
    $("storeCount").textContent = String(storeNames().length);
  }

  async function openPath(path) {
    if (!path) return;
    try {
      const result = await HQYL.api().open_path(path);
      if (!result.ok) throw new Error(result.error || "打开文件失败");
    } catch (error) { HQYL.showToast(error.message || String(error)); }
  }

  function appendCell(row, value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = String(value);
    cell.className = className;
    row.appendChild(cell);
    return cell;
  }

  function amountText(value) {
    if (value === null || value === undefined || value === "" || (typeof value === "number" && !Number.isFinite(value))) return "未取得";
    // Preserve the returned decimal text, including zero, negatives and currency precision.
    return String(value);
  }

  function evidenceFiles(evidence) {
    if (!evidence) return [];
    const entries = Array.isArray(evidence) ? evidence.map((value, index) => [`截图 ${index + 1}`, value]) : Object.entries(evidence);
    return entries.flatMap(([label, item]) => {
      const path = typeof item === "string" ? item : item?.path || item?.screenshot || "";
      return path ? [{ path, label: item?.label || label }] : [];
    });
  }

  function renderRows(stores, emptyText = "没有店铺结果") {
    const body = $("storeResults");
    body.replaceChildren();
    if (!stores.length) {
      const row = document.createElement("tr");
      appendCell(row, emptyText).colSpan = columnCount;
      body.appendChild(row);
      return;
    }
    stores.forEach(store => {
      const row = document.createElement("tr");
      appendCell(row, store.store_name || store.requested_store_name || "—");
      appendCell(row, [countries[store.country] || store.country, store.currency].filter(Boolean).join(" / ") || "—");
      appendCell(row, statusNames[store.status] || store.status || "未知", `balance-status-${store.status || "unknown"}`);
      fields.forEach(field => {
        const exempt = field === "processing" && store.values?.[field] == null
          ? { withdrawal_success: "最新一笔提现已成功，无需记录处理中金额", no_withdrawal: "当前余额流水日期范围内无提现记录，具体范围见说明" }[store.evidence_exemptions?.processing]
          : "";
        const text = exempt ? "无需记录" : amountText(store.values?.[field]);
        const cell = appendCell(row, text, `balance-amount${text === "未取得" ? " balance-missing" : ""}`);
        if (exempt) cell.title = exempt;
      });
      appendCell(row, store.captured_at || "—");
      const evidenceCell = appendCell(row, "");
      const files = evidenceFiles(store.evidence);
      if (!files.length) evidenceCell.textContent = "未取得";
      else {
        const list = document.createElement("div");
        list.className = "balance-evidence";
        files.forEach((file, index) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "secondary compact-btn";
          button.textContent = `截图 ${index + 1}`;
          button.title = file.label;
          button.setAttribute("aria-label", `${store.store_name || "店铺"} ${file.label}`);
          button.addEventListener("click", () => openPath(file.path));
          list.appendChild(button);
        });
        evidenceCell.appendChild(list);
      }
      appendCell(row, [store.message, ...(Array.isArray(store.notes) ? store.notes : [])].filter(Boolean).join("；") || "—");
      body.appendChild(row);
    });
  }

  function renderResult(result) {
    snapshot = { ...result, account_id: result.account_id || activeId() };
    const stores = Array.isArray(result.stores) ? result.stores : [];
    const summary = result.summary || {};
    const success = summary.success ?? stores.filter(store => store.status === "success").length;
    const partial = summary.partial ?? stores.filter(store => ["partial", "partial_success"].includes(store.status)).length;
    const failed = summary.failed ?? stores.filter(store => !["success", "partial", "partial_success"].includes(store.status)).length;
    const total = summary.total ?? stores.length;
    $("recordCount").textContent = String(success);
    $("attentionCount").textContent = String(partial + failed);
    $("attentionSummary").textContent = `部分 ${partial} / 失败 ${failed}`;
    $("resultCount").textContent = `共 ${total} 家`;
    $("outputFile").textContent = result.output_file ? (result.is_complete ? "已生成" : "部分结果") : "未生成";
    $("resultMessage").textContent = result.message || result.completion_message || `共 ${total} 家，成功 ${success} 家，部分完成 ${partial} 家，失败 ${failed} 家。${result.is_complete ? "统计 Excel 与截图已保存。" : "请查看说明并重试未完成店铺。"}`;
    $("taskBadge").className = `status-pill ${result.is_complete ? "success" : "failed"}`;
    $("taskBadge").textContent = result.is_complete ? "已完成" : "结果不完整";
    $("statusText").textContent = result.is_complete ? "金额与截图已保存" : "请查看店铺结果中的说明";
    renderRows(stores);
    controls();
  }

  async function start(retry = false) {
    if (busy()) return;
    const id = activeId();
    if (!id) { HQYL.openAccountDialog("ziniao", () => start(retry)); return; }
    if (retry && (!snapshot?.manifest_path || snapshot.account_id !== id)) return;
    const previous = snapshot;
    const payload = retry ? { platform, account_id: id, manifest_path: snapshot.manifest_path } : {
      platform, account_id: id, store_names: storeNames().join("\n"), output_dir: $("balanceOutputDir").value.trim(),
      country: isTemu ? "AUTO" : $("balanceCountry").value,
      ...(isTemu ? { month: $("balanceMonth").value } : {}),
    };
    showIssue("");
    const task = await HQYL.startTask(async () => {
      HQYL.appendLog(retry ? `准备重试 ${title} 未完成店铺` : `准备启动 ${title}，按顺序逐店采集`);
      try {
        const result = await HQYL.api()[retry ? "retry_balance_statistics" : "start_balance_statistics"](payload);
        if (!result.ok) throw new Error(result.error || "任务启动失败");
        return result;
      } catch (error) {
        if (id === activeId()) showIssue(error.message || String(error));
        throw error;
      }
    }, () => id === activeId());
    if (!task && retry && previous && id === activeId()) renderResult(previous);
    controls();
  }

  async function loadInfo(resetMonth = false) {
    const monthBeforeRequest = isTemu ? $("balanceMonth").value : "";
    try {
      const info = await HQYL.api().get_balance_statistics_info({ platform });
      if (!info.ok) throw new Error(info.error || "统计默认值加载失败");
      if (isTemu && (resetMonth || $("balanceMonth").value === monthBeforeRequest)) {
        $("balanceMonth").value = info.month || previousMonth();
      }
      if (!$("balanceOutputDir").value) $("balanceOutputDir").value = info.output_dir || info.settings?.output_dir || "";
      if (!isTemu && Array.isArray(info.countries) && info.countries.length) {
        const select = $("balanceCountry");
        const previous = select.value || "AUTO";
        select.replaceChildren();
        [{ code: "AUTO", name: "自动识别每家店铺" }, ...info.countries.filter(country => country.code !== "AUTO")].forEach(country => {
          const option = document.createElement("option");
          option.value = country.code;
          option.textContent = country.name || countries[country.code] || country.code;
          select.appendChild(option);
        });
        select.value = Array.from(select.options).some(option => option.value === previous) ? previous : "AUTO";
      }
      if (resetMonth) HQYL.showToast("已恢复上一个自然月");
    } catch (error) { showIssue(error.message || String(error)); }
    controls();
  }

  const page = {
    key: `${platform}_balance_statistics`, taskKey: `${platform}_balance_statistics`, title,
    taskContextFilter: context => Boolean(activeId()) && context.account_id === activeId() && context.platform === platform,
    async init(info) {
      accountId = activeId();
      $("balanceOutputDir").value = info.settings?.output_dir || "";
      if (isTemu) $("balanceMonth").value = previousMonth();
      $("balanceForm").addEventListener("submit", event => { event.preventDefault(); void start(); });
      ["storeNames", "balanceOutputDir", "balanceMonth", "balanceCountry"].forEach(id => {
        if ($(id)) $(id).addEventListener("input", () => { showIssue(""); controls(); });
      });
      $("chooseOutputBtn").addEventListener("click", async () => {
        if (busy()) return;
        preparing = true; controls();
        try {
          const result = await HQYL.api().choose_output_dir($("balanceOutputDir").value);
          if (result.ok) $("balanceOutputDir").value = result.path;
          else if (!result.cancelled) throw new Error(result.error || "选择目录失败");
        } catch (error) { showIssue(error.message || String(error)); }
        finally { preparing = false; controls(); }
      });
      if ($("resetMonthBtn")) $("resetMonthBtn").addEventListener("click", async () => {
        if (busy()) return;
        preparing = true; controls();
        try { await loadInfo(true); } finally { preparing = false; controls(); }
      });
      $("openFileBtn").addEventListener("click", () => openPath(snapshot?.output_file));
      $("openDirectoryBtn").addEventListener("click", () => openPath(snapshot?.output_dir));
      $("retryBtn").addEventListener("click", () => start(true));
      initialized = true;
      await loadInfo();
      controls();
    },
    onAccountsChanged() {
      if (!initialized || accountId === activeId()) return;
      accountId = activeId(); running = false;
      $("storeNames").value = "";
      page.resetResult();
      showIssue("");
      $("logBox").textContent = "";
      $("taskBadge").className = "status-pill idle";
      $("taskBadge").textContent = "待运行";
      $("statusText").textContent = "已切换紫鸟账号";
      $("resultMessage").textContent = "已切换账号，请填写当前账号的店铺名称。";
    },
    setRunning(value) { running = value; controls(); },
    resetResult() {
      snapshot = null;
      $("recordCount").textContent = "0";
      $("attentionCount").textContent = "0";
      $("attentionSummary").textContent = "部分 0 / 失败 0";
      $("outputFile").textContent = "未生成";
      $("resultCount").textContent = "共 0 家";
      $("resultMessage").textContent = "正在准备采集，请查看运行日志。";
      renderRows([], "等待店铺统计结果");
      controls();
    },
    applyResult: renderResult,
  };
  HQYL.boot(page);
})();
