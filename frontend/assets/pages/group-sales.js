(() => {
  const $ = HQYL.$;
  let resultOutputDir = "";
  let outputFile = "";
  let groups = [];
  let selectedIds = new Set();
  let loading = false;
  let running = false;
  let initialized = false;
  let accountId = "";
  let loadSequence = 0;
  let loadError = "";

  function selectedGroupIds() {
    return groups.filter((group) => selectedIds.has(group.id)).map((group) => group.id);
  }

  function visibleGroups() {
    const keyword = $("salesGroupSearch").value.trim().toLowerCase();
    return groups.filter((group) => !keyword || group.name.toLowerCase().includes(keyword) || group.id.toLowerCase().includes(keyword));
  }

  function updateGroupCount() {
    const count = selectedGroupIds().length;
    $("skuCount").textContent = String(count);
    $("groupSelectionSummary").textContent = `共 ${groups.length} 个分类 · 当前显示 ${visibleGroups().length} 个 · 已选 ${count} 个`;
    $("clearGroupsBtn").disabled = running || loading || !count;
  }

  function updateControls() {
    const unavailable = running || loading || !groups.length;
    $("groupRunBtn").disabled = unavailable;
    $("groupOutputDir").disabled = running;
    $("groupChooseOutputBtn").disabled = running;
    $("reloadGroupsBtn").disabled = running || loading;
    $("reloadGroupsBtn").textContent = loading ? "正在加载…" : "重新加载";
    $("salesGroupSearch").disabled = unavailable;
    $("selectAllGroupsBtn").disabled = unavailable || !visibleGroups().length;
    $("selectAllGroupsBtn").textContent = $("salesGroupSearch").value.trim() ? "全选搜索结果" : "全选全部分类";
    $("salesGroupSelect").querySelectorAll("input").forEach((input) => { input.disabled = unavailable; });
    updateGroupCount();
  }

  function renderGroups() {
    const list = $("salesGroupSelect");
    list.textContent = "";
    const visible = visibleGroups();
    if (loading || loadError || !visible.length) {
      const message = document.createElement("div");
      message.className = "developer-empty";
      message.textContent = loading ? "正在从马帮读取全部自定义分类…" : loadError || (groups.length ? "没有匹配的自定义分类，请尝试其他关键词" : "请先绑定马帮账号，再点击重新加载");
      list.appendChild(message);
    } else visible.forEach((group) => {
      const label = document.createElement("label");
      label.className = "developer-option";
      label.title = `${group.name}（${group.id}）`;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = group.id;
      input.checked = selectedIds.has(group.id);
      const name = document.createElement("span");
      name.textContent = group.name;
      label.append(input, name);
      list.appendChild(label);
    });
    updateControls();
  }

  async function loadGroups() {
    if (loading) return;
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", loadGroups);
      return;
    }
    const sequence = ++loadSequence;
    loading = true;
    loadError = "";
    renderGroups();
    try {
      const response = await HQYL.api().get_group_sales_options({ account_id: account.id });
      if (sequence !== loadSequence) return;
      if (!response.ok) throw new Error(response.error || "自定义分类加载失败");
      groups = (response.groups || []).map((group) => ({ id: String(group.id), name: String(group.name) }));
      // 进入报表或重新加载时都不自动勾选分类，也不恢复上次运行的选择；仅保留本次页面上仍存在的手动勾选。
      selectedIds = new Set(selectedGroupIds());
      if (!groups.length) loadError = "当前账号没有可用的自定义分类，请确认权限后重新加载";
    } catch (error) {
      if (sequence !== loadSequence) return;
      groups = [];
      loadError = `分类加载失败：${error.message || String(error)}。请点击重新加载。`;
    } finally {
      if (sequence === loadSequence) {
        loading = false;
        renderGroups();
      }
    }
  }

  async function runReport() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runReport);
      return;
    }
    if (loading || running) return;
    if (!selectedGroupIds().length) {
      HQYL.showToast("请选择至少一个自定义分类");
      return;
    }
    const outputRoot = $("groupOutputDir").value.trim();
    if (!outputRoot) {
      HQYL.showToast("请输入输出目录");
      $("groupOutputDir").focus();
      return;
    }
    await HQYL.startTask(async () => {
      HQYL.appendLog("准备启动菲律宾各组商品销量报表导出");
      const task = await HQYL.api().start_group_sales_report({
        account_id: account.id,
        start_date: $("groupStartDate").value,
        end_date: $("groupEndDate").value,
        output_dir: outputRoot,
        group_ids: selectedGroupIds(),
      });
      if (task.ok && task.context?.output_root) $("groupOutputDir").value = task.context.output_root;
      return task;
    });
  }

  async function chooseOutputDirectory() {
    try {
      const result = await HQYL.api().choose_output_dir($("groupOutputDir").value.trim());
      if (result.ok) $("groupOutputDir").value = result.path;
      else if (!result.cancelled) HQYL.showToast(result.error || "选择目录失败");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  const page = {
    key: "group_sales",
    taskKey: "group_sales",
    title: "菲律宾各组销量报表",
    async init(info) {
      const settings = info.settings || {};
      const dates = info.date_range || {};
      $("groupOutputDir").value = settings.output_dir || "D:\\报表\\";
      $("groupStartDate").value = dates.start_date || "";
      $("groupEndDate").value = dates.end_date || "";
      // 每次进入报表都从空选择开始，不沿用配置文件里上一次运行保存的分类。
      selectedIds = new Set();
      accountId = HQYL.activeAccount("mabang")?.id || "";
      initialized = true;
      $("groupSalesForm").addEventListener("submit", (event) => { event.preventDefault(); return runReport(); });
      $("salesGroupSearch").addEventListener("input", renderGroups);
      $("salesGroupSearch").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
      $("salesGroupSelect").addEventListener("change", (event) => {
        const input = event.target;
        if (input.type !== "checkbox") return;
        if (input.checked) selectedIds.add(input.value);
        else selectedIds.delete(input.value);
        updateGroupCount();
      });
      $("reloadGroupsBtn").addEventListener("click", loadGroups);
      $("selectAllGroupsBtn").addEventListener("click", () => { visibleGroups().forEach((group) => selectedIds.add(group.id)); renderGroups(); });
      $("clearGroupsBtn").addEventListener("click", () => { selectedIds.clear(); renderGroups(); });
      $("groupChooseOutputBtn").addEventListener("click", chooseOutputDirectory);
      $("groupOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || resultOutputDir));
      renderGroups();
      if (accountId) loadGroups();
    },
    onAccountsChanged() {
      const nextAccountId = HQYL.activeAccount("mabang")?.id || "";
      if (!initialized || nextAccountId === accountId) return;
      accountId = nextAccountId;
      ++loadSequence;
      loading = false;
      groups = [];
      selectedIds.clear();
      loadError = "";
      $("salesGroupSearch").value = "";
      renderGroups();
      if (accountId) loadGroups();
    },
    setRunning(value) {
      running = value;
      updateControls();
    },
    resetResult() {
      updateGroupCount();
      $("groupOpenOutputBtn").disabled = true;
      outputFile = "";
      resultOutputDir = "";
    },
    applyResult(result) {
      $("skuCount").textContent = String(result.group_count ?? selectedGroupIds().length);
      $("recordCount").textContent = String(result.record_count ?? 0);
      outputFile = result.output_file || "";
      // Restored results must never replace the user's next export root.
      resultOutputDir = result.output_dir || "";
      $("outputFile").textContent = outputFile || "未生成";
      $("groupOpenOutputBtn").disabled = !outputFile && !resultOutputDir;
    },
  };

  HQYL.boot(page);
})();
