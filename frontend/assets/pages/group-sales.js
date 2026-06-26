(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";

  function selectedGroupIds() {
    return Array.from($("salesGroupSelect").selectedOptions).map((option) => option.value);
  }

  function updateGroupCount() {
    $("skuCount").textContent = String(selectedGroupIds().length);
  }

  function renderGroups(groups, savedIds) {
    const selected = new Set(savedIds || []);
    $("salesGroupSelect").textContent = "";
    groups.forEach((group, index) => {
      const option = document.createElement("option");
      option.value = group.id;
      option.textContent = group.name;
      option.selected = selected.size ? selected.has(group.id) : index === 0;
      $("salesGroupSelect").appendChild(option);
    });
    updateGroupCount();
  }

  async function runReport() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runReport);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备启动菲律宾各组商品销量报表导出");
      return HQYL.api().start_group_sales_report({
        account_id: account.id,
        start_date: $("groupStartDate").value,
        end_date: $("groupEndDate").value,
        output_dir: outputDir,
        group_ids: selectedGroupIds(),
      });
    });
  }

  const page = {
    key: "group_sales",
    taskKey: "group_sales",
    title: "菲律宾各组销量报表",
    async init(info) {
      const settings = info.settings || {};
      const dates = info.date_range || {};
      outputDir = settings.output_dir || "";
      $("groupStartDate").value = dates.start_date || "";
      $("groupEndDate").value = dates.end_date || "";
      renderGroups((info.group_sales_report || {}).groups || [], settings.sales_group_ids || []);
      $("groupSalesForm").addEventListener("submit", (event) => { event.preventDefault(); runReport(); });
      $("salesGroupSelect").addEventListener("change", updateGroupCount);
      $("selectAllGroupsBtn").addEventListener("click", () => { Array.from($("salesGroupSelect").options).forEach((option) => { option.selected = true; }); updateGroupCount(); });
      $("clearGroupsBtn").addEventListener("click", () => { Array.from($("salesGroupSelect").options).forEach((option) => { option.selected = false; }); updateGroupCount(); });
      $("groupOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
    },
    setRunning(running) {
      ["groupRunBtn", "selectAllGroupsBtn", "clearGroupsBtn"].forEach((id) => { $(id).disabled = running; });
    },
    resetResult() {
      updateGroupCount();
      $("groupOpenOutputBtn").disabled = true;
      outputFile = "";
    },
    applyResult(result) {
      $("skuCount").textContent = String(result.group_count ?? selectedGroupIds().length);
      $("recordCount").textContent = String(result.record_count ?? 0);
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = outputFile || "未生成";
      $("groupOpenOutputBtn").disabled = !outputFile && !outputDir;
    },
  };

  HQYL.boot(page);
})();
