(() => {
  const $ = HQYL.$;
  let outputDir = "";

  function storeCount() {
    return $("lazadaStores").value.split(/\r?\n/).filter((line) => line.trim()).length;
  }

  function updateStoreCount() {
    $("skuCount").textContent = String(storeCount());
  }

  function renderCountries(countries) {
    const select = $("lazadaCountry");
    const previous = select.value;
    select.textContent = "";
    (countries || []).forEach((country) => {
      const option = document.createElement("option");
      option.value = country.code;
      option.textContent = country.name;
      select.appendChild(option);
    });
    if (previous && Array.from(select.options).some((option) => option.value === previous)) select.value = previous;
  }

  function payload(account) {
    const company = account && account.extra ? account.extra.company || "" : "";
    return {
      account_id: account ? account.id : "",
      company,
      username: account ? account.username : "",
      password: "",
      country: $("lazadaCountry").value,
      start_date: $("lazadaStartDate").value,
      end_date: $("lazadaEndDate").value,
      store_names: $("lazadaStores").value,
      max_concurrent_stores: $("lazadaConcurrent").value,
      browser_window_mode: $("lazadaWindowMode").value,
      client_path: $("lazadaClientPath").value,
      webdriver_path: $("lazadaDriverPath").value,
    };
  }

  async function runStatistics() {
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", runStatistics);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备启动 Lazada 提现统计");
      return HQYL.api().start_lazada_withdrawal_statistics(payload(account));
    });
  }

  async function refreshInfo(showMessage = true) {
    try {
      const result = await HQYL.api().get_lazada_withdrawal_statistics_info();
      if (!result.ok) throw new Error(result.error || "默认配置加载失败");
      renderCountries(result.countries || []);
      const period = result.period || {};
      $("lazadaStartDate").value = period.start_date || "";
      $("lazadaEndDate").value = period.end_date || "";
      $("lazadaClientPath").value = result.client_path || "";
      $("lazadaDriverPath").value = result.webdriver_path || "";
      if (showMessage) HQYL.showToast("已恢复上一个自然月和运行路径");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function choosePath(method, targetId, message) {
    try {
      const result = await HQYL.api()[method]();
      if (result.ok) $(targetId).value = result.path;
      else if (!result.cancelled) throw new Error(result.error || message);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  function statusText(status) {
    return {
      success: "成功",
      no_data: "无流水（已上传空表 Excel）",
      partial_success: "部分成功",
      no_matching_statement: "无匹配账单",
      page_unavailable: "页面未开放",
      unmatched_store: "未匹配店铺",
      need_manual_review: "需人工复核",
      failed: "失败",
    }[status] || status || "未知";
  }

  function appendCell(row, text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.appendChild(cell);
  }

  function renderResults(stores) {
    const body = $("lazadaResultBody");
    body.textContent = "";
    if (!Array.isArray(stores) || stores.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = "没有店铺结果";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    stores.forEach((store) => {
      const row = document.createElement("tr");
      appendCell(row, store.store_name || store.requested_store_name || "—");
      appendCell(row, store.country || "—");
      appendCell(row, statusText(store.status));
      const hasWithdrawalWorkbook = store.country !== "TH"
        && (store.local_files || []).some((file) => String(file).toLowerCase().endsWith(".xlsx"));
      const recordSummary = hasWithdrawalWorkbook && Number.isInteger(store.record_count)
        ? `1 个 Excel / ${store.record_count} 行`
        : `${(store.local_files || []).length} 个`;
      appendCell(row, recordSummary);
      appendCell(row, (store.drive_paths || []).join("；") || store.message || "—");
      body.appendChild(row);
    });
  }

  const page = {
    key: "lazada_withdrawal_statistics",
    taskKey: "lazada_withdrawal_statistics",
    title: "Lazada 提现统计",
    async init(info) {
      outputDir = (info.settings || {}).output_dir || "";
      await refreshInfo(false);
      $("lazadaWithdrawalForm").addEventListener("submit", (event) => { event.preventDefault(); runStatistics(); });
      $("lazadaStores").addEventListener("input", updateStoreCount);
      $("lazadaRefreshBtn").addEventListener("click", () => refreshInfo(true));
      $("chooseClientPathBtn").addEventListener("click", () => choosePath("choose_client_path", "lazadaClientPath", "选择客户端失败"));
      $("chooseDriverPathBtn").addEventListener("click", () => choosePath("choose_driver_path", "lazadaDriverPath", "选择驱动目录失败"));
      $("lazadaOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputDir));
      updateStoreCount();
    },
    setRunning(running) {
      ["lazadaRunBtn", "lazadaRefreshBtn", "chooseClientPathBtn", "chooseDriverPathBtn"].forEach((id) => { $(id).disabled = running; });
    },
    resetResult() {
      $("matchedCount").textContent = "0";
      $("failedCount").textContent = "失败 0";
      $("lazadaOpenOutputBtn").disabled = true;
      renderResults([]);
      updateStoreCount();
    },
    applyResult(result) {
      $("matchedCount").textContent = String(result.matched_store_count ?? 0);
      $("recordCount").textContent = String(result.success_store_count ?? 0);
      $("failedCount").textContent = `失败 ${result.failed_store_count ?? 0} / 部分 ${result.partial_store_count ?? 0}`;
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = result.output_file ? "结果已生成" : "未生成";
      $("lazadaOpenOutputBtn").disabled = !outputDir;
      renderResults(result.stores || []);
    },
  };

  HQYL.boot(page);
})();
