(() => {
  const $ = HQYL.$;
  let resultOutputDir = "";
  let operatorNames = [];
  const preferenceFields = { client_path: "lazadaAdsClientPath", workbook_id: "lazadaAdsWorkbookId" };
  const preferenceRevisions = { client_path: 0, workbook_id: 0 };
  const preferenceEdited = { client_path: false, workbook_id: false };
  const savedPreferences = {};
  const preferenceErrors = {};
  let preferenceSaveQueue = Promise.resolve(true);
  let pendingPreferenceSaves = 0;
  let startingCollection = false;

  function preferenceValue(key) {
    const value = $(preferenceFields[key]).value.trim();
    return key === "client_path" ? value.replace(/^"+|"+$/g, "") : value;
  }

  function renderPreferenceStatus() {
    const hint = $("lazadaAdsPreferencesStatus");
    const errors = Object.values(preferenceErrors);
    const dirtyKeys = Object.keys(preferenceFields).filter((key) => preferenceValue(key) !== savedPreferences[key]);
    hint.classList.toggle("error-text", errors.length > 0);
    hint.textContent = errors.length ? `保存失败：${errors[0]}。请修改后重试，或再次点击开始采集。`
      : pendingPreferenceSaves ? "正在保存客户端路径和工作簿配置…"
      : dirtyKeys.some((key) => preferenceEdited[key]) ? "配置已修改，离开输入框后自动保存。"
      : dirtyKeys.length ? "选择或修改后自动保存；当前未保存项使用系统默认配置。"
      : "客户端路径和工作簿配置已保存，下次打开自动使用。";
  }

  function savePreferences(keys = Object.keys(preferenceFields)) {
    const snapshot = Object.fromEntries(keys.map((key) => [key, preferenceValue(key)]));
    pendingPreferenceSaves += 1;
    renderPreferenceStatus();
    const operation = preferenceSaveQueue.then(async () => {
      const changed = Object.fromEntries(Object.entries(snapshot).filter(([key, value]) => savedPreferences[key] !== value));
      if (!Object.keys(changed).length) return true;
      try {
        const result = await HQYL.api().save_lazada_ads_preferences(changed);
        if (!result.ok) throw new Error(result.error || "配置保存失败");
        Object.entries(changed).forEach(([key, value]) => {
          savedPreferences[key] = value;
          if (preferenceValue(key) === value) preferenceEdited[key] = false;
          delete preferenceErrors[key];
        });
        return true;
      } catch (error) {
        Object.entries(changed).forEach(([key, value]) => {
          if (preferenceValue(key) === value) preferenceErrors[key] = error.message || String(error);
        });
        return false;
      }
    });
    preferenceSaveQueue = operation.finally(() => {
      pendingPreferenceSaves -= 1;
      renderPreferenceStatus();
    });
    return preferenceSaveQueue;
  }

  function storeNames() {
    const seen = new Set();
    return String($("lazadaAdsStores").value || "").split(/\r?\n/).map((name) => name.trim()).filter((name) => {
      const key = name.toLocaleLowerCase();
      if (!name || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function updateStoreCount() {
    $("skuCount").textContent = String(storeNames().length);
  }

  function renderOperators(names) {
    operatorNames = Array.isArray(names) ? names.map(String).filter(Boolean) : [];
    const list = $("lazadaAdsOperatorList");
    list.replaceChildren();
    operatorNames.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      list.appendChild(option);
    });
    if (!$("lazadaAdsOperatorName").value && operatorNames.length === 1) $("lazadaAdsOperatorName").value = operatorNames[0];
  }

  function buildPayload(account) {
    return {
      account_id: account ? account.id : "",
      sheet_name: $("lazadaAdsSheetName").value.trim(),
      target_date: $("lazadaAdsTargetDate").value,
      store_names: storeNames(),
      workbook_id: $("lazadaAdsWorkbookId").value.trim(),
      dingtalk_operator_name: $("lazadaAdsOperatorName").value.trim(),
      browser_window_mode: $("lazadaAdsWindowMode").value,
      client_path: preferenceValue("client_path"),
      output_root: $("lazadaAdsOutputDir").value,
    };
  }

  async function runCollection() {
    if (startingCollection) return;
    const payload = buildPayload(HQYL.activeAccount("ziniao"));
    const missing = !payload.store_names.length ? "请输入至少一个店铺名称"
      : !payload.workbook_id ? "请输入钉钉工作簿 ID"
      : !payload.dingtalk_operator_name ? "请填写钉钉操作人姓名"
      : !payload.output_root ? "请选择输出目录" : "";
    if (missing) { HQYL.showToast(missing); return; }
    if (!payload.account_id) { HQYL.openAccountDialog("ziniao", runCollection); return; }
    startingCollection = true;
    page.setRunning(true);
    try {
      if (!await savePreferences()) { page.setRunning(false); return; }
      await HQYL.startTask(() => {
        HQYL.appendLog(`准备采集 ${payload.target_date || "昨天"} 的泰国 Lazada 广告费与业绩，指定 Sheet：${payload.sheet_name || "昨天所在月份"}`);
        HQYL.appendLog(`店铺数量：${payload.store_names.length}`);
        return HQYL.api().start_lazada_ads_data(payload);
      });
    } finally {
      startingCollection = false;
    }
  }

  async function refreshInfo(showMessage = true) {
    const revisions = { ...preferenceRevisions };
    try {
      if (showMessage && !await savePreferences()) return;
      await preferenceSaveQueue;
      const result = await HQYL.api().get_lazada_ads_data_info();
      if (!result.ok) throw new Error(result.error || "默认配置加载失败");
      const runtime = result.runtime || result;
      $("lazadaAdsSheetName").value = "";
      $("lazadaAdsSheetName").placeholder = result.default_sheet_name ? `留空使用：${result.default_sheet_name}` : "留空使用当前月份";
      $("lazadaAdsTargetDate").value = "";
      const preferences = { workbook_id: result.workbook_id || result.default_workbook_id || "", client_path: runtime.client_path || "" };
      Object.entries(preferences).forEach(([key, value]) => {
        if (preferenceRevisions[key] !== revisions[key]) return;
        $(preferenceFields[key]).value = value;
        savedPreferences[key] = String((result.saved_preferences || {})[key] || "").trim();
        preferenceEdited[key] = false;
        delete preferenceErrors[key];
      });
      renderPreferenceStatus();
      $("lazadaAdsWindowMode").value = "normal";
      if (result.output_dir) $("lazadaAdsOutputDir").value = result.output_dir;
      if (result.default_dingtalk_operator_name) $("lazadaAdsOperatorName").value = result.default_dingtalk_operator_name;
      renderOperators(result.operator_names || operatorNames);
      if (showMessage) HQYL.showToast("已恢复默认配置，Sheet 留空使用昨天所在月份，日期留空使用昨天");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function choosePath(method, targetId, failureMessage, withCurrentPath = false) {
    const clientRevision = preferenceRevisions.client_path;
    try {
      const result = withCurrentPath ? await HQYL.api()[method]($(targetId).value) : await HQYL.api()[method]();
      if (result.ok) {
        if (targetId === "lazadaAdsClientPath" && preferenceRevisions.client_path !== clientRevision) return;
        $(targetId).value = result.path || "";
        if (targetId === "lazadaAdsClientPath") {
          preferenceRevisions.client_path += 1;
          preferenceEdited.client_path = true;
          delete preferenceErrors.client_path;
          await savePreferences(["client_path"]);
        }
      }
      else if (!result.cancelled) throw new Error(result.error || failureMessage);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  function statusText(status) {
    return {
      success: "成功", collected: "采集成功", synced: "已写入 Sheet", partial_success: "部分成功",
      skipped: "已存在，跳过", skip: "已存在，跳过",
      no_data: "无数据", unmatched_store: "未匹配店铺", failed: "失败", collection_failed: "采集失败",
      sync_failed: "Sheet 写入失败", write_failed: "Sheet 写入失败", need_manual_review: "需人工复核",
    }[status] || status || "未知";
  }

  function money(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : String(value);
  }

  function sumAmount(stores, field) {
    const amounts = stores.map((store) => store[field]).filter((value) => value !== null && value !== undefined && value !== "").map(Number).filter(Number.isFinite);
    return amounts.length ? amounts.reduce((sum, value) => sum + value, 0) : null;
  }

  function renderResults(stores, targetDate = "") {
    const body = $("lazadaAdsResultBody");
    body.replaceChildren();
    if (!stores.length) {
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
      [store.store_name || store.requested_store_name || "—", store.target_date || targetDate || "—", money(store.advertising), money(store.performance), statusText(store.status), store.message || "—"].forEach((text) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  const page = {
    key: "lazada_ads_data",
    taskKey: "lazada_ads_data",
    title: "泰国 Lazada 广告数据",
    async init(info) {
      const today = new Date();
      const pad = (value) => String(value).padStart(2, "0");
      $("lazadaAdsTargetDate").max = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
      $("lazadaAdsOutputDir").value = (info.settings || {}).output_dir || "";
      renderOperators(((info.settings || {}).dingtalk || {}).operator_names || []);
      Object.entries(preferenceFields).forEach(([key, id]) => {
        $(id).addEventListener("input", () => {
          preferenceRevisions[key] += 1;
          preferenceEdited[key] = true;
          delete preferenceErrors[key];
          renderPreferenceStatus();
        });
        $(id).addEventListener("change", () => savePreferences([key]));
      });
      await refreshInfo(false);
      $("lazadaAdsDataForm").addEventListener("submit", (event) => { event.preventDefault(); runCollection(); });
      $("lazadaAdsStores").addEventListener("input", updateStoreCount);
      $("lazadaAdsRefreshBtn").addEventListener("click", () => refreshInfo(true));
      $("lazadaAdsChooseClientBtn").addEventListener("click", () => choosePath("choose_client_path", "lazadaAdsClientPath", "选择客户端失败"));
      $("lazadaAdsChooseOutputBtn").addEventListener("click", () => choosePath("choose_output_dir", "lazadaAdsOutputDir", "选择输出目录失败", true));
      $("lazadaAdsOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(resultOutputDir || $("lazadaAdsOutputDir").value));
      updateStoreCount();
    },
    setRunning(running) {
      $("lazadaAdsDataForm").querySelectorAll("input, select, textarea, button").forEach((element) => {
        if (element.id !== "lazadaAdsOpenOutputBtn") element.disabled = running;
      });
    },
    resetResult() {
      $("matchedCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("lazadaAdsAdvertisingTotal").textContent = "—";
      $("lazadaAdsPerformanceTotal").textContent = "—";
      $("failedCount").textContent = "失败 0";
      $("lazadaAdsOpenOutputBtn").disabled = true;
      resultOutputDir = "";
      renderResults([]);
      updateStoreCount();
    },
    applyResult(result) {
      const rawStores = result.stores || result.results || result.store_results || [];
      const stores = Array.isArray(rawStores) ? rawStores : [];
      const successful = stores.filter((store) => ["success", "collected", "synced"].includes(store.status)).length;
      const failed = stores.filter((store) => ["failed", "collection_failed", "unmatched_store", "sync_failed", "write_failed"].includes(store.status)).length;
      $("matchedCount").textContent = String(result.matched_store_count ?? stores.filter((store) => store.status !== "unmatched_store").length);
      $("recordCount").textContent = String(result.success_store_count ?? successful);
      $("lazadaAdsAdvertisingTotal").textContent = money(result.total_advertising ?? sumAmount(stores, "advertising"));
      $("lazadaAdsPerformanceTotal").textContent = money(result.total_performance ?? sumAmount(stores, "performance"));
      $("failedCount").textContent = `失败 ${result.failed_store_count ?? failed} / 跳过 ${result.skipped_store_count ?? stores.filter((store) => ["skipped", "skip"].includes(store.status)).length}`;
      resultOutputDir = result.output_dir || "";
      $("outputFile").textContent = result.output_file || result.message || (resultOutputDir ? "本地结果已保存" : "未生成本地结果");
      $("lazadaAdsOpenOutputBtn").disabled = !resultOutputDir;
      renderResults(stores, result.target_date || $("lazadaAdsTargetDate").value);
    },
  };

  HQYL.boot(page);
})();
