(() => {
  const $ = HQYL.$;
  const FALLBACK_COUNTRIES = [
    { code: "TH", name: "泰国" },
    { code: "MY", name: "马来西亚" },
    { code: "PH", name: "菲律宾" },
  ];
  let outputRoot = "";
  let resultOutputDir = "";

  function storeNames() {
    const seen = new Set();
    return String($("lazadaMonthlyStores").value || "")
      .split(/\r?\n/)
      .map((name) => name.trim())
      .filter((name) => {
        const key = name.toLocaleLowerCase();
        if (!name || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  function updateStoreCount() {
    $("skuCount").textContent = String(storeNames().length);
  }

  function countryLabel(code) {
    const option = Array.from($("lazadaMonthlyCountry").options).find((item) => item.value === code);
    return option ? option.textContent : code || "—";
  }

  function renderCountries(countries, defaultCountry = "") {
    const select = $("lazadaMonthlyCountry");
    const previous = select.value;
    const options = Array.isArray(countries) && countries.length ? countries : FALLBACK_COUNTRIES;
    select.replaceChildren();
    options.forEach((country) => {
      const option = document.createElement("option");
      option.value = String(country.code || country.value || "").toUpperCase();
      option.textContent = country.name || country.label || option.value;
      select.appendChild(option);
    });
    const preferred = String(defaultCountry || previous || "").toUpperCase();
    if (preferred && Array.from(select.options).some((option) => option.value === preferred)) {
      select.value = preferred;
    }
  }

  function buildPayload(account) {
    return {
      account_id: account ? account.id : "",
      country: $("lazadaMonthlyCountry").value,
      month: $("lazadaMonthlyMonth").value,
      store_names: $("lazadaMonthlyStores").value,
      browser_window_mode: $("lazadaMonthlyWindowMode").value,
      client_path: $("lazadaMonthlyClientPath").value,
      output_dir: $("lazadaMonthlyOutputDir").value,
    };
  }

  async function runReport() {
    if (!$("lazadaMonthlyMonth").value) {
      HQYL.showToast("请选择账单月份");
      return;
    }
    if (!storeNames().length) {
      HQYL.showToast("请输入至少一个店铺名称");
      return;
    }
    if (!outputRoot) {
      HQYL.showToast("请选择输出目录");
      return;
    }
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", runReport);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备下载 ${countryLabel($("lazadaMonthlyCountry").value)} ${$("lazadaMonthlyMonth").value} 月度账单`);
      HQYL.appendLog(`店铺数量：${storeNames().length}`);
      return HQYL.api().start_lazada_monthly_report(buildPayload(account));
    });
  }

  async function refreshInfo(showMessage = true) {
    try {
      const result = await HQYL.api().get_lazada_monthly_report_info();
      if (!result.ok) throw new Error(result.error || "默认配置加载失败");
      renderCountries(result.countries, result.default_country);
      $("lazadaMonthlyMonth").value = result.default_month || "";
      $("lazadaMonthlyMonth").max = result.default_month || "";
      $("lazadaMonthlyClientPath").value = result.client_path || "";
      if (showMessage) HQYL.showToast("已恢复上个月和运行路径");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function choosePath(method, targetId, failureMessage, currentValue = null) {
    try {
      const result = currentValue === null
        ? await HQYL.api()[method]()
        : await HQYL.api()[method](currentValue);
      if (result.ok) {
        $(targetId).value = result.path || "";
        return result.path || "";
      }
      if (!result.cancelled) throw new Error(result.error || failureMessage);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
    return "";
  }

  async function chooseOutputDir() {
    const selected = await choosePath("choose_output_dir", "lazadaMonthlyOutputDir", "选择输出目录失败", outputRoot);
    if (selected) outputRoot = selected;
  }

  function statusText(status) {
    return {
      success: "成功",
      downloaded: "下载成功",
      partial_success: "部分成功",
      no_data: "无数据",
      no_report: "无匹配月报",
      no_matching_report: "无匹配月报",
      no_matching_statement: "无匹配月报",
      page_unavailable: "页面未开放",
      unmatched_store: "未匹配店铺",
      need_manual_review: "需人工复核",
      download_failed: "下载失败",
      filename_collision: "文件名冲突",
      failed: "失败",
    }[status] || status || "未知";
  }

  function fileName(path) {
    const text = String(path || "");
    return text.split(/[\\/]/).pop() || "—";
  }

  function appendCell(row, text, title = "") {
    const cell = document.createElement("td");
    cell.textContent = text;
    if (title) cell.title = title;
    row.appendChild(cell);
  }

  function resultFile(store) {
    const files = Array.isArray(store.local_files) ? store.local_files : [];
    return store.output_file || store.local_file || store.file_path || files[0] || "";
  }

  function renderResults(stores, defaultMonth = "") {
    const body = $("lazadaMonthlyResultBody");
    body.replaceChildren();
    if (!Array.isArray(stores) || !stores.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent = "没有店铺结果";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    stores.forEach((store) => {
      const row = document.createElement("tr");
      const path = resultFile(store);
      const country = store.country_name || countryLabel(String(store.country || $("lazadaMonthlyCountry").value).toUpperCase());
      appendCell(row, store.store_name || store.requested_store_name || "—");
      appendCell(row, country);
      appendCell(row, store.month || defaultMonth || $("lazadaMonthlyMonth").value || "—");
      appendCell(row, statusText(store.status));
      appendCell(row, fileName(path), path);
      appendCell(row, store.message || (path ? "账单已保存" : "—"));
      body.appendChild(row);
    });
  }

  const page = {
    key: "lazada_monthly_report",
    taskKey: "lazada_monthly_report",
    title: "Lazada 月度账单下载",
    async init(info) {
      outputRoot = (info.settings || {}).output_dir || "";
      $("lazadaMonthlyOutputDir").value = outputRoot;
      await refreshInfo(false);
      $("lazadaMonthlyReportForm").addEventListener("submit", (event) => { event.preventDefault(); runReport(); });
      $("lazadaMonthlyStores").addEventListener("input", updateStoreCount);
      $("lazadaMonthlyRefreshBtn").addEventListener("click", () => refreshInfo(true));
      $("lazadaMonthlyChooseClientBtn").addEventListener("click", () => choosePath("choose_client_path", "lazadaMonthlyClientPath", "选择客户端失败"));
      $("lazadaMonthlyChooseOutputBtn").addEventListener("click", chooseOutputDir);
      $("lazadaMonthlyOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(resultOutputDir || outputRoot));
      updateStoreCount();
    },
    setRunning(running) {
      [
        "lazadaMonthlyCountry",
        "lazadaMonthlyMonth",
        "lazadaMonthlyStores",
        "lazadaMonthlyWindowMode",
        "lazadaMonthlyRunBtn",
        "lazadaMonthlyRefreshBtn",
        "lazadaMonthlyChooseClientBtn",
        "lazadaMonthlyChooseOutputBtn",
      ].forEach((id) => { $(id).disabled = running; });
    },
    resetResult() {
      $("matchedCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("failedCount").textContent = "失败 0";
      $("lazadaMonthlyOpenOutputBtn").disabled = true;
      resultOutputDir = "";
      renderResults([]);
      updateStoreCount();
    },
    applyResult(result) {
      const stores = result.stores || result.results || result.store_results || [];
      const successful = Array.isArray(stores)
        ? stores.filter((store) => ["success", "downloaded"].includes(store.status)).length
        : 0;
      const failed = Array.isArray(stores)
        ? stores.filter((store) => ["failed", "download_failed", "unmatched_store"].includes(store.status)).length
        : 0;
      const unmatched = Number(result.unmatched_store_count ?? 0);
      const matched = result.matched_store_count
        ?? result.processed_store_count
        ?? Math.max(0, stores.length - unmatched);
      const successCount = result.success_store_count ?? result.downloaded_store_count ?? successful;
      $("matchedCount").textContent = String(matched);
      $("recordCount").textContent = String(successCount);
      $("failedCount").textContent = `失败 ${result.failed_store_count ?? failed} / 部分 ${result.partial_store_count ?? 0}`;
      resultOutputDir = result.output_dir || "";
      $("outputFile").textContent = successCount ? `${successCount} 个账单` : (resultOutputDir ? "目录已生成" : "未生成");
      $("lazadaMonthlyOpenOutputBtn").disabled = !resultOutputDir && !outputRoot;
      renderResults(stores, result.month || $("lazadaMonthlyMonth").value);
    },
  };

  HQYL.boot(page);
})();
