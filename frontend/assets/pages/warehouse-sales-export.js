(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";
  let running = false;
  let initialized = false;
  let accountId = "";
  let loadSequence = 0;

  function createChecklist(options) {
    const state = { items: [], selected: new Set(), loading: false, error: "" };

    function locked() {
      return running || state.loading;
    }

    function selectedIds() {
      return state.items.filter((item) => state.selected.has(item.id)).map((item) => item.id);
    }

    function visible() {
      const keyword = $(options.searchId).value.trim().toLowerCase();
      return state.items.filter((item) => !keyword || item.name.toLowerCase().includes(keyword) || item.id.toLowerCase().includes(keyword));
    }

    function update() {
      const count = selectedIds().length;
      const disabled = locked();
      $(options.summaryId).textContent = `共 ${state.items.length} 个${options.noun} · 当前显示 ${visible().length} 个 · 已选 ${count} 个`;
      $(options.searchId).disabled = disabled;
      $(options.listId).querySelectorAll("input").forEach((input) => { input.disabled = disabled; });
      $(options.clearId).disabled = disabled || !count;
      $(options.selectAllId).disabled = disabled || !visible().length;
      $(options.selectAllId).textContent = $(options.searchId).value.trim() ? "全选搜索结果" : `全选全部${options.noun}`;
      options.onChange();
    }

    function render() {
      const list = $(options.listId);
      list.textContent = "";
      const items = visible();
      if (state.loading || state.error || !items.length) {
        const message = document.createElement("div");
        message.className = "developer-empty";
        message.textContent = state.loading
          ? `正在从马帮读取全部${options.noun}…`
          : state.error || (state.items.length ? `没有匹配的${options.noun}，请尝试其他关键词` : "请先绑定马帮账号，再点击重新加载");
        list.appendChild(message);
      } else {
        items.forEach((item) => {
          const label = document.createElement("label");
          label.className = "developer-option";
          label.title = `${item.name}（${item.id}）`;
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = item.id;
          input.checked = state.selected.has(item.id);
          const name = document.createElement("span");
          name.textContent = item.name;
          label.append(input, name);
          list.appendChild(label);
        });
      }
      update();
    }

    function setItems(items) {
      state.items = items.map((item) => ({ id: String(item.id), name: String(item.name) }));
      state.selected = new Set(selectedIds());
    }

    function toggle(input) {
      if (input.checked) state.selected.add(input.value);
      else state.selected.delete(input.value);
      update();
    }

    function selectAllVisible() {
      visible().forEach((item) => state.selected.add(item.id));
      render();
    }

    function clear() {
      state.selected.clear();
      render();
    }

    function reset() {
      state.items = [];
      state.selected = new Set();
      state.loading = false;
      state.error = "";
      $(options.searchId).value = "";
      render();
    }

    $(options.searchId).addEventListener("input", render);
    $(options.searchId).addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
    $(options.listId).addEventListener("change", (event) => {
      const input = event.target;
      if (input.type !== "checkbox") return;
      toggle(input);
    });
    $(options.selectAllId).addEventListener("click", selectAllVisible);
    $(options.clearId).addEventListener("click", clear);

    return { state, selectedIds, render, setItems, reset, update };
  }

  const categoryList = createChecklist({
    listId: "categorySelect",
    searchId: "categorySearch",
    summaryId: "categorySummary",
    selectAllId: "selectAllCategoriesBtn",
    clearId: "clearCategoriesBtn",
    noun: "分类",
    onChange: () => updateControls(),
  });

  const warehouseList = createChecklist({
    listId: "warehouseSelect",
    searchId: "warehouseSearch",
    summaryId: "warehouseSummary",
    selectAllId: "selectAllWarehousesBtn",
    clearId: "clearWarehousesBtn",
    noun: "仓库",
    onChange: () => updateControls(),
  });

  function updateControls() {
    const loading = categoryList.state.loading || warehouseList.state.loading;
    const empty = !categoryList.state.items.length && !warehouseList.state.items.length;
    $("warehouseRunBtn").disabled = running || loading || empty;
    const reloadLabel = loading ? "正在加载…" : "重新加载";
    $("reloadCategoriesBtn").disabled = running || loading;
    $("reloadWarehousesBtn").disabled = running || loading;
    $("reloadCategoriesBtn").textContent = reloadLabel;
    $("reloadWarehousesBtn").textContent = reloadLabel;
    $("categoryCount").textContent = `${categoryList.selectedIds().length} / ${warehouseList.selectedIds().length}`;
  }

  async function loadOptions() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", loadOptions);
      return;
    }
    const sequence = ++loadSequence;
    categoryList.state.loading = true;
    warehouseList.state.loading = true;
    categoryList.state.error = "";
    warehouseList.state.error = "";
    categoryList.render();
    warehouseList.render();
    updateControls();
    try {
      const response = await HQYL.api().get_warehouse_export_options({ account_id: account.id });
      if (sequence !== loadSequence) return;
      if (!response.ok) throw new Error(response.error || "分类与仓库加载失败");
      categoryList.setItems(response.categories || []);
      warehouseList.setItems(response.warehouses || []);
      if (response.output_dir) outputDir = response.output_dir;
      if (!categoryList.state.items.length) categoryList.state.error = "当前账号没有可用分类，请确认权限后重新加载";
      if (!warehouseList.state.items.length) warehouseList.state.error = "当前账号没有可用仓库，请确认权限后重新加载";
    } catch (error) {
      if (sequence !== loadSequence) return;
      categoryList.reset();
      warehouseList.reset();
      categoryList.state.error = `加载失败：${error.message || String(error)}。请点击重新加载。`;
    } finally {
      if (sequence === loadSequence) {
        categoryList.state.loading = false;
        warehouseList.state.loading = false;
        categoryList.render();
        warehouseList.render();
        updateControls();
      }
    }
  }

  async function runExport() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runExport);
      return;
    }
    if (running) return;
    const categoryIds = categoryList.selectedIds();
    const warehouseIds = warehouseList.selectedIds();
    if (!categoryIds.length && !warehouseIds.length) {
      HQYL.showToast("请至少选择一个自定义分类或一个仓库");
      return;
    }
    const startMonth = $("warehouseStartMonth").value;
    const endMonth = $("warehouseEndMonth").value;
    if (!startMonth || !endMonth) {
      HQYL.showToast("请选择开始月份和结束月份");
      return;
    }
    if (startMonth > endMonth) {
      HQYL.showToast("开始月份不能晚于结束月份");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备启动商品仓库报表按月导出");
      return HQYL.api().start_warehouse_sales_export({
        account_id: account.id,
        start_month: startMonth,
        end_month: endMonth,
        output_dir: outputDir,
        category_ids: categoryIds,
        warehouse_ids: warehouseIds,
      });
    });
  }

  const page = {
    key: "warehouse_sales_export",
    taskKey: "warehouse_sales_export",
    title: "商品仓库报表按月导出",
    async init(info) {
      const detail = info.warehouse_sales_export || {};
      const months = detail.month_range || {};
      outputDir = detail.output_dir || "";
      $("warehouseStartMonth").value = months.start_month || "";
      $("warehouseEndMonth").value = months.end_month || "";
      accountId = HQYL.activeAccount("mabang")?.id || "";
      initialized = true;
      $("warehouseExportForm").addEventListener("submit", (event) => { event.preventDefault(); return runExport(); });
      $("reloadCategoriesBtn").addEventListener("click", loadOptions);
      $("reloadWarehousesBtn").addEventListener("click", loadOptions);
      $("warehouseOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
      categoryList.render();
      warehouseList.render();
      updateControls();
      if (accountId) loadOptions();
      else updateControls();
    },
    onAccountsChanged() {
      const nextAccountId = HQYL.activeAccount("mabang")?.id || "";
      if (!initialized || nextAccountId === accountId) return;
      accountId = nextAccountId;
      ++loadSequence;
      categoryList.reset();
      warehouseList.reset();
      updateControls();
      if (accountId) loadOptions();
    },
    setRunning(value) {
      running = value;
      categoryList.update();
      warehouseList.update();
      updateControls();
    },
    resetResult() {
      $("recordCount").textContent = "0";
      $("outputFile").textContent = "未生成";
      $("statusText").textContent = "等待任务启动";
      $("warehouseOpenOutputBtn").disabled = true;
      outputFile = "";
      updateControls();
    },
    applyResult(result) {
      const months = result.months || [];
      const failed = result.failed_months || [];
      $("recordCount").textContent = String(result.record_count ?? 0);
      $("categoryCount").textContent = `${result.category_count ?? categoryList.selectedIds().length} / ${result.warehouse_count ?? warehouseList.selectedIds().length}`;
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = outputFile || "未生成";
      const summary = `已完成 ${result.month_count ?? months.length} 个月`;
      $("statusText").textContent = failed.length ? `${summary}，${failed.length} 个月失败：${failed.join("、")}` : summary;
      $("warehouseOpenOutputBtn").disabled = !outputFile && !outputDir;
    },
  };

  HQYL.boot(page);
})();
