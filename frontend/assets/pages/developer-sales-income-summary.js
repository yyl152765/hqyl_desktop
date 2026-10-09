(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";

  function developerItems() {
    const seen = new Set();
    return $("developerNames").value.split(/\r?\n/).map((item) => item.trim()).filter((item) => {
      const key = item.toLowerCase();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function updateInputCount() {
    $("inputDeveloperCount").textContent = $("includeAllPeople").checked ? "全部" : String(developerItems().length);
  }

  function syncAllPeopleMode() {
    const includeAll = $("includeAllPeople").checked;
    $("developerNames").disabled = includeAll;
    $("developerNames").required = !includeAll;
    updateInputCount();
  }

  async function runSummary() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runSummary);
      return;
    }
    const names = developerItems();
    const includeAll = $("includeAllPeople").checked;
    if (!includeAll && !names.length) {
      HQYL.showToast("请按一行一个姓名输入至少一名开发员");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(includeAll ? "准备遍历全部开发员和销售员" : `准备统计 ${names.length} 个输入姓名`);
      HQYL.appendLog(`付款日期：${$("incomeSummaryStartDate").value} 至 ${$("incomeSummaryEndDate").value}`);
      return HQYL.api().start_developer_sales_income_summary({
        account_id: account.id,
        developer_names: names,
        include_all: includeAll,
        start_date: $("incomeSummaryStartDate").value,
        end_date: $("incomeSummaryEndDate").value,
        output_dir: outputDir,
      });
    });
  }

  const page = {
    key: "developer_sales_income_summary",
    taskKey: "developer_sales_income_summary",
    title: "开发与销售收入汇总导出",
    async init(info) {
      const settings = info.settings || {};
      const dates = info.date_range || {};
      outputDir = settings.output_dir || "";
      $("incomeSummaryStartDate").value = dates.start_date || "";
      $("incomeSummaryEndDate").value = dates.end_date || "";
      $("developerSalesIncomeForm").addEventListener("submit", (event) => { event.preventDefault(); runSummary(); });
      $("developerNames").addEventListener("input", updateInputCount);
      $("includeAllPeople").addEventListener("change", syncAllPeopleMode);
      $("clearDeveloperNamesBtn").addEventListener("click", () => {
        $("developerNames").value = "";
        updateInputCount();
      });
      $("incomeSummaryOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
      syncAllPeopleMode();
    },
    setRunning(running) {
      ["incomeSummaryRunBtn", "clearDeveloperNamesBtn", "includeAllPeople", "incomeSummaryStartDate", "incomeSummaryEndDate"]
        .forEach((id) => { $(id).disabled = running; });
      $("developerNames").disabled = running || $("includeAllPeople").checked;
    },
    resetResult() {
      outputFile = "";
      $("developerCount").textContent = "0 名开发员";
      $("salespersonCount").textContent = "0 名销售员";
      $("outputFile").textContent = "未生成";
      $("incomeSummaryOpenOutputBtn").disabled = true;
      updateInputCount();
    },
    applyResult(result) {
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      $("developerCount").textContent = `${result.developer_count ?? 0} 名开发员`;
      $("salespersonCount").textContent = `${result.salesperson_count ?? 0} 名销售员 · ${result.source_row_count ?? 0} 行来源数据`;
      $("outputFile").textContent = outputFile || "未生成";
      $("incomeSummaryOpenOutputBtn").disabled = !outputFile && !outputDir;
      const missingNames = Array.isArray(result.missing_developer_names) ? result.missing_developer_names : [];
      if (missingNames.length) HQYL.showToast(`已跳过 ${missingNames.length} 个不存在的开发员姓名`);
    },
  };

  HQYL.boot(page);
})();
