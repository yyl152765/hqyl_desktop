(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";
  let groups = [];
  let selectedIds = new Set();
  let loading = false;
  let running = false;
  let initialized = false;
  let restoredSelection = false;
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
      const hadSavedSelection = selectedIds.size > 0;
      selectedIds = new Set(selectedGroupIds());
      if (!restoredSelection && !hadSavedSelection && groups.length) selectedIds.add(groups[0].id);
      restoredSelection = true;
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
      selectedIds = new Set((settings.sales_group_ids || []).map(String));
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
      $("groupOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
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
      restoredSelection = false;
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
