(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";
  let rowsPerPage = 500;

  function skuItems() {
    return $("skuText").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  }

  function updateSkuCount() {
    $("skuCount").textContent = String(skuItems().length);
  }

  async function runQuery() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runQuery);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备启动 SKU 采购日志查询");
      return HQYL.api().start_purchase_log_query({
        account_id: account.id,
        start_date: $("startDate").value,
        end_date: $("endDate").value,
        rows_per_page: rowsPerPage,
        output_dir: outputDir,
        sku_text: $("skuText").value,
      });
    });
  }

  const page = {
    key: "purchase_log",
    taskKey: "purchase_log",
    title: "SKU 采购日志查询",
    async init(info) {
      const settings = info.settings || {};
      const dates = info.date_range || {};
      outputDir = settings.output_dir || "";
      rowsPerPage = Number(settings.rows_per_page || 500);
      $("startDate").value = dates.start_date || "";
      $("endDate").value = dates.end_date || "";
      $("purchaseLogForm").addEventListener("submit", (event) => { event.preventDefault(); runQuery(); });
      $("skuText").addEventListener("input", updateSkuCount);
      $("clearSkuBtn").addEventListener("click", () => { $("skuText").value = ""; updateSkuCount(); });
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
      updateSkuCount();
    },
    setRunning(running) {
      $("runBtn").disabled = running;
      $("clearSkuBtn").disabled = running;
    },
    resetResult() {
      updateSkuCount();
      $("openOutputBtn").disabled = true;
      outputFile = "";
    },
    applyResult(result) {
      $("skuCount").textContent = String(result.sku_count ?? skuItems().length);
      $("recordCount").textContent = String(result.record_count ?? 0);
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = outputFile || "未生成";
      $("openOutputBtn").disabled = !outputFile && !outputDir;
    },
  };

  HQYL.boot(page);
})();
