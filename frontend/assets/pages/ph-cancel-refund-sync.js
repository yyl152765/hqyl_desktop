(() => {
  const { $ } = HQYL;
  const tool = "ph_cancel_refund_sync";
  let accountId = "";
  let running = false;
  let token = "";
  let outputFile = "";

  function updateControls() {
    $("phPreviewBtn").disabled = running;
    $("phOutputDir").disabled = running;
    $("phChooseDirBtn").disabled = running;
    $("phOpenResultBtn").disabled = !outputFile;
    $("phConfirmation").hidden = !token;
    $("phConfirm").disabled = running;
    $("phSyncBtn").disabled = running || !token || !$("phConfirm").checked;
  }

  function clearConfirmation() {
    token = "";
    $("phConfirm").checked = false;
    updateControls();
  }

  async function chooseDirectory() {
    const result = await HQYL.api().choose_scoped_output_dir({ tool, current_dir: $("phOutputDir").value });
    if (result.ok) {
      $("phOutputDir").value = result.path || result.output_dir;
      clearConfirmation();
    } else if (!result.cancelled) {
      HQYL.showToast(result.error || "无法选择输出目录");
    }
  }

  async function preview() {
    if (running) return;
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", preview);
      return;
    }
    await HQYL.startTask(() => HQYL.api().start_ph_cancel_refund_preview({
      account_id: account.id,
      output_dir: $("phOutputDir").value.trim(),
    }));
  }

  async function synchronize() {
    const account = HQYL.activeAccount("mabang");
    if (running || !account || !token || !$("phConfirm").checked) return;
    const payload = { account_id: account.id, preview_token: token, confirm_sync: true };
    await HQYL.startTask(() => HQYL.api().start_ph_cancel_refund_sync(payload));
  }

  function renderRows(rows) {
    const body = $("phOrderRows");
    body.replaceChildren();
    for (const row of rows) {
      const tr = body.insertRow();
      for (const key of ["order_id", "shop_name", "status", "reason"]) {
        tr.insertCell().textContent = row[key] || "—";
      }
    }
  }

  const page = {
    key: tool,
    taskKey: tool,
    title: "菲律宾取消订单同步退款",
    init(info) {
      accountId = HQYL.activeAccount("mabang")?.id || "";
      $("phOutputDir").value = info.ph_cancel_refund_sync?.output_dir || "";
      $("phRefundForm").addEventListener("submit", (event) => { event.preventDefault(); preview(); });
      $("phSyncBtn").addEventListener("click", synchronize);
      $("phConfirm").addEventListener("change", updateControls);
      $("phChooseDirBtn").addEventListener("click", () => chooseDirectory().catch(error => HQYL.showToast(error.message)));
      $("phOutputDir").addEventListener("input", clearConfirmation);
      $("phOpenResultBtn").addEventListener("click", () => HQYL.openOutput(outputFile));
      updateControls();
    },
    taskContextFilter(context) {
      return Boolean(accountId) && context.account_id === accountId;
    },
    onAccountsChanged() {
      const nextAccount = HQYL.activeAccount("mabang")?.id || "";
      if (nextAccount === accountId) return;
      accountId = nextAccount;
      running = false;
      page.resetResult();
    },
    setRunning(value) {
      running = value;
      updateControls();
    },
    resetResult() {
      outputFile = "";
      for (const id of ["shopCount", "orderCount", "acceptedCount", "remainingCount"]) $(id).textContent = "—";
      $("phResultSummary").textContent = "查询后显示订单";
      $("phStatusHeader").textContent = "订单状态";
      $("phReasonHeader").textContent = "异常说明";
      renderRows([]);
      clearConfirmation();
    },
    applyResult(result) {
      if (!result.mode) return;
      const rows = result.rows || [];
      const total = result.order_count || 0;
      outputFile = result.output_file || "";
      $("shopCount").textContent = result.shop_count ?? "—";
      $("orderCount").textContent = total;
      $("acceptedCount").textContent = result.accepted_count ?? "—";
      $("remainingCount").textContent = result.remaining_count ?? "—";
      const syncResult = result.mode === "sync";
      $("phStatusHeader").textContent = syncResult ? "同步回执" : "订单状态";
      $("phReasonHeader").textContent = syncResult ? "异常列表复查" : "异常说明";
      let summary = total ? `显示 ${rows.length} / ${total} 单` : "当前异常列表无待同步订单";
      if (total > rows.length) summary += "，完整明细见结果表";
      if (syncResult && result.review_count) summary += `，${result.review_count} 单回执待处理`;
      if (result.preview) summary = `演示数据 · ${summary}`;
      $("phResultSummary").textContent = summary;
      renderRows(rows);
      token = !syncResult && total > 0 ? result.preview_token || "" : "";
      $("phConfirmText").textContent = `确认同步预览的 ${total} 单（15 分钟内有效）`;
      updateControls();
    },
  };

  HQYL.boot(page);
})();
