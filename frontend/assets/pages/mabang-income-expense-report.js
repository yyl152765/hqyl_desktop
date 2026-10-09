(() => {
  const $ = HQYL.$;
  const YACANG_WAREHOUSE_KEYS = new Set(["1100204", "1100500", "1096191"]);
  let outputDir = "";
  let outputFiles = [];

  function selectedCategoryIds() {
    return Array.from($("incomeExpenseCategorySelect").selectedOptions).map((option) => option.value);
  }

  function selectedWarehouseKeys() {
    return Array.from($("incomeExpenseWarehouseSelect").selectedOptions).map((option) => option.value);
  }

  function updateSelectionCount() {
    $("selectionCount").textContent = String(
      selectedCategoryIds().length + selectedWarehouseKeys().length,
    );
  }

  function renderCategories(categories, savedIds) {
    const saved = new Set(savedIds || []);
    const select = $("incomeExpenseCategorySelect");
    select.textContent = "";
    (categories || []).forEach((category) => {
      const option = document.createElement("option");
      option.value = category.id;
      option.textContent = category.name;
      option.title = category.mabang_name && category.mabang_name !== category.name
        ? `马帮分类：${category.mabang_name}`
        : category.name;
      option.selected = saved.has(category.id);
      select.appendChild(option);
    });
    updateSelectionCount();
  }

  function renderWarehouses(warehouses) {
    const select = $("incomeExpenseWarehouseSelect");
    select.textContent = "";
    (warehouses || []).forEach((warehouse) => {
      const option = document.createElement("option");
      option.value = warehouse.key;
      option.textContent = warehouse.note
        ? `${warehouse.name}（${warehouse.note}）`
        : warehouse.name;
      const mabangNames = Array.isArray(warehouse.mabang_names) ? warehouse.mabang_names.join("、") : "";
      option.title = mabangNames ? `马帮仓库：${mabangNames}` : warehouse.name;
      option.selected = false;
      select.appendChild(option);
    });
    updateSelectionCount();
  }

  function selectOptions(selectId, predicate) {
    Array.from($(selectId).options).forEach((option) => {
      option.selected = predicate(option);
    });
    updateSelectionCount();
  }

  async function runReport() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runReport);
      return;
    }
    const categoryIds = selectedCategoryIds();
    const warehouseKeys = selectedWarehouseKeys();
    if (!categoryIds.length && !warehouseKeys.length) {
      HQYL.showToast("请选择至少一个店铺自定义分类或海外仓");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备导出：分类 ${categoryIds.length} 个，海外仓 ${warehouseKeys.length} 个`);
      HQYL.appendLog(`发货时间：${$("incomeExpenseStartDate").value} 至 ${$("incomeExpenseEndDate").value}`);
      return HQYL.api().start_mabang_income_expense_report({
        account_id: account.id,
        start_date: $("incomeExpenseStartDate").value,
        end_date: $("incomeExpenseEndDate").value,
        output_dir: outputDir,
        category_ids: categoryIds,
        warehouse_keys: warehouseKeys,
      });
    });
  }

  const page = {
    key: "mabang_income_expense_report",
    taskKey: "mabang_income_expense_report",
    title: "马帮收支报表",
    async init(info) {
      const settings = info.settings || {};
      const reportInfo = info.mabang_income_expense_report || {};
      const dates = reportInfo.date_range || info.date_range || {};
      outputDir = settings.output_dir || "";
      $("incomeExpenseStartDate").value = dates.start_date || "";
      $("incomeExpenseEndDate").value = dates.end_date || "";
      renderCategories(reportInfo.categories || [], settings.income_expense_category_ids || []);
      renderWarehouses(reportInfo.warehouses || []);

      $("incomeExpenseForm").addEventListener("submit", (event) => {
        event.preventDefault();
        runReport();
      });
      $("incomeExpenseCategorySelect").addEventListener("change", updateSelectionCount);
      $("incomeExpenseWarehouseSelect").addEventListener("change", updateSelectionCount);
      $("selectYacangWarehousesBtn").addEventListener("click", () => selectOptions(
        "incomeExpenseWarehouseSelect",
        (option) => YACANG_WAREHOUSE_KEYS.has(option.value),
      ));
      $("selectAllWarehousesBtn").addEventListener("click", () => selectOptions("incomeExpenseWarehouseSelect", () => true));
      $("selectAllCategoriesBtn").addEventListener("click", () => selectOptions("incomeExpenseCategorySelect", () => true));
      $("clearSelectionsBtn").addEventListener("click", () => {
        selectOptions("incomeExpenseCategorySelect", () => false);
        selectOptions("incomeExpenseWarehouseSelect", () => false);
      });
      $("incomeExpenseOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputDir));
    },
    setRunning(running) {
      [
        "incomeExpenseRunBtn",
        "selectYacangWarehousesBtn",
        "selectAllWarehousesBtn",
        "selectAllCategoriesBtn",
        "clearSelectionsBtn",
        "incomeExpenseCategorySelect",
        "incomeExpenseWarehouseSelect",
        "incomeExpenseStartDate",
        "incomeExpenseEndDate",
      ].forEach((id) => { $(id).disabled = running; });
    },
    resetResult() {
      outputFiles = [];
      $("recordCount").textContent = "0";
      $("fileCount").textContent = "0 个";
      $("incomeExpenseOpenOutputBtn").disabled = true;
      updateSelectionCount();
    },
    applyResult(result) {
      outputFiles = Array.isArray(result.output_files) ? result.output_files : [];
      outputDir = result.output_dir || outputDir;
      $("selectionCount").textContent = String(
        result.target_count ?? selectedCategoryIds().length + selectedWarehouseKeys().length,
      );
      $("recordCount").textContent = String(result.record_count ?? 0);
      $("fileCount").textContent = `${result.csv_file_count ?? outputFiles.length} 个`;
      $("incomeExpenseOpenOutputBtn").disabled = !outputDir;
    },
  };

  HQYL.boot(page);
})();
