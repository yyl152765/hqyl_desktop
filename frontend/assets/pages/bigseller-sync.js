(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";

  function syncTypeLabel() {
    return $("syncType").value === "product" ? "同步产品" : "同步库存";
  }

  function listingStatusLabel() {
    return $("listingStatus").value === "soldOut" ? "售完" : "在售";
  }

  function updateSummary() {
    $("syncTypeText").textContent = syncTypeLabel();
    $("listingStatusText").textContent = listingStatusLabel();
  }

  async function chooseOutputDir() {
    const result = await HQYL.api().choose_output_dir($("outputDir").value || outputDir);
    if (result.ok) {
      outputDir = result.path;
      $("outputDir").value = result.path;
    } else if (!result.cancelled) {
      HQYL.showToast(result.error || "选择目录失败");
    }
  }

  async function runSync() {
    const account = HQYL.activeAccount("bigseller");
    if (!account) {
      HQYL.openAccountDialog("bigseller", runSync);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备启动 BigSeller ${listingStatusLabel()}${syncTypeLabel()}`);
      return HQYL.api().start_bigseller_sync({
        account_id: account.id,
        sync_type: $("syncType").value,
        listing_status: $("listingStatus").value,
        page_size: Number($("pageSize").value || 300),
        max_rounds: Number($("maxRounds").value || 0),
        output_dir: $("outputDir").value.trim() || outputDir,
      });
    });
  }

  const page = {
    key: "bigseller_sync",
    taskKey: "bigseller_sync",
    title: "BigSeller 同步",
    async init(info) {
      const settings = info.settings || {};
      outputDir = settings.output_dir || "";
      $("outputDir").value = outputDir;
      $("bigsellerSyncForm").addEventListener("submit", (event) => { event.preventDefault(); runSync(); });
      $("syncType").addEventListener("change", updateSummary);
      $("listingStatus").addEventListener("change", updateSummary);
      $("chooseDirBtn").addEventListener("click", chooseOutputDir);
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
      updateSummary();
    },
    setRunning(running) {
      $("runBtn").disabled = running;
      $("syncType").disabled = running;
      $("listingStatus").disabled = running;
      $("pageSize").disabled = running;
      $("maxRounds").disabled = running;
      $("chooseDirBtn").disabled = running;
    },
    resetResult() {
      $("openOutputBtn").disabled = true;
      $("recordCount").textContent = "0";
      outputFile = "";
    },
    applyResult(result) {
      $("recordCount").textContent = String(result.processed_count ?? 0);
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = outputFile || "未生成";
      $("openOutputBtn").disabled = !outputFile && !outputDir;
    },
  };

  HQYL.boot(page);
})();
