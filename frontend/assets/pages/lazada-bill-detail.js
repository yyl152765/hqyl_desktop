(() => {
  const $ = HQYL.$;
  let resultOutputDir = "";
  let operatorNames = [];
  let countryProfiles = {};
  let activeCountry = "";
  let defaultWorkbookId = "";
  let defaultSheetName = "";
  let preferredSheetName = "";
  let loadingSheets = false;
  const savedPreferences = { client_path: "", screenshot_root: "", workbook_id: "" };
  const preferenceErrors = {};
  const PREFERENCE_TARGETS = {
    client_path: { id: "lazadaBillClientPath", saved: () => savedPreferences.client_path || "" },
    screenshot_root: { id: "lazadaBillScreenshotDir", saved: () => savedPreferences.screenshot_root || "" },
    workbook_id: { id: "lazadaBillWorkbookId", saved: () => savedPreferences.workbook_id || defaultWorkbookId },
  };
  const preferenceRevisions = { client_path: 0, screenshot_root: 0, workbook_id: 0 };
  let preferenceSaveQueue = Promise.resolve(true);
  let pendingPreferenceSaves = 0;
  let startingCollection = false;

  const todayIso = () => {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  };

  function currentCountry() {
    return $("lazadaBillCountry").value || "";
  }

  function countryProfile(code = currentCountry()) {
    return countryProfiles[code] || {};
  }

  function workbookIdValue() {
    return $("lazadaBillWorkbookId").value.trim();
  }

  function preferenceValue(key) {
    return $(PREFERENCE_TARGETS[key].id).value.trim();
  }

  function renderPreferenceStatus() {
    const hint = $("lazadaBillPreferencesStatus");
    const errors = Object.values(preferenceErrors);
    const dirty = Object.keys(PREFERENCE_TARGETS).some((key) => preferenceValue(key) !== PREFERENCE_TARGETS[key].saved());
    hint.classList.toggle("error-text", errors.length > 0);
    hint.textContent = errors.length ? `保存失败：${errors[0]}。请修改后重试，或再次点击开始采集。`
      : pendingPreferenceSaves ? "正在保存钉钉文档、客户端路径和截图目录…"
      : dirty ? "配置已修改，离开输入框后自动保存。"
      : "钉钉文档 ID、客户端路径和截图根目录已保存，下次打开自动使用。";
  }

  let dwsAvailable = true;
  let dwsHint = "";

  function renderDwsWarning() {
    const banner = $("lazadaBillDwsWarning");
    if (!banner) return;
    banner.hidden = dwsAvailable;
    $("lazadaBillDwsHint").textContent = dwsAvailable ? "" : dwsHint;
  }

  function renderImageHint() {
    const node = workbookIdValue() || defaultWorkbookId;
    const target = node ? `${node} 的 F 列` : "同一文档的 F 列";
    const state = dwsAvailable ? "" : "；未检测到 dws，图片不会写入";
    $("lazadaBillImageHint").textContent = `钉钉截图写入：${target}（不会写入 A～E 列）${state}`;
  }

  function renderScreenshotHint() {
    const root = $("lazadaBillScreenshotDir").value.trim();
    const name = countryProfile().name;
    const start = $("lazadaBillStartDate").value;
    const end = $("lazadaBillEndDate").value;
    $("lazadaBillScreenshotHint").textContent = root && name && start && end
      ? `本次截图保存目录：${root}\\Lazada${name}账单明细截图\\${start}到${end}`
      : "本次截图保存目录：按「国家 + 账单区间」在截图根目录下自动创建";
  }

  function persistDefaultWorkbook() {
    if (savedPreferences.workbook_id || !defaultWorkbookId) return;
    savedPreferences.workbook_id = defaultWorkbookId;
    preferenceSaveQueue = preferenceSaveQueue.then(async () => {
      const result = await HQYL.api().save_lazada_bill_preferences({ workbook_id: defaultWorkbookId });
      if (!result.ok) throw new Error(result.error || "默认钉钉文档保存失败");
      return true;
    }).catch((error) => {
      savedPreferences.workbook_id = "";
      HQYL.appendLog(`默认钉钉文档保存失败：${error.message || error}`);
      return false;
    });
    preferenceSaveQueue.finally(renderPreferenceStatus);
  }

  function savePreferences(keys = Object.keys(PREFERENCE_TARGETS)) {
    const snapshot = Object.fromEntries(keys.map((key) => [key, preferenceValue(key)]));
    pendingPreferenceSaves += 1;
    renderPreferenceStatus();
    const operation = preferenceSaveQueue.then(async () => {
      const changed = Object.entries(snapshot).filter(([key, value]) => PREFERENCE_TARGETS[key].saved() !== value);
      if (!Object.keys(changed).length) return true;
      const payload = {};
      changed.forEach(([key, value]) => { payload[key] = value; });
      try {
        const result = await HQYL.api().save_lazada_bill_preferences(payload);
        if (!result.ok) throw new Error(result.error || "配置保存失败");
        changed.forEach(([key, value]) => {
          savedPreferences[key] = value;
          delete preferenceErrors[key];
        });
        renderImageHint();
        return true;
      } catch (error) {
        changed.forEach(([key, value]) => {
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

  function renderSheetOptions(names) {
    const select = $("lazadaBillSheetName");
    const previous = select.value;
    const available = Array.isArray(names) ? names : [];
    select.replaceChildren();
    const fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = defaultSheetName ? `默认（${defaultSheetName}）` : "默认（当前月份）";
    select.appendChild(fallback);
    available.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });
    // 默认优先选与国家同名的 Sheet（泰国/菲律宾/马来/印尼/越南），再看月份，最后保留用户上次选择。
    const target = [preferredSheetName, defaultSheetName, previous].find((name) => name && available.includes(name));
    if (target) select.value = target;
  }

  async function loadSheets(showMessage = false) {
    if (loadingSheets) return;
    loadingSheets = true;
    try {
      const result = await HQYL.api().list_lazada_bill_sheets({
        country: currentCountry(),
        workbook_id: workbookIdValue(),
        dingtalk_operator_name: $("lazadaBillOperatorName").value.trim(),
      });
      if (!result.ok) throw new Error(result.error || "读取 Sheet 列表失败");
      defaultSheetName = result.default_sheet_name || defaultSheetName;
      preferredSheetName = result.preferred_sheet_name || "";
      renderSheetOptions(result.sheet_names || []);
      if (showMessage) {
        const selected = $("lazadaBillSheetName").value;
        HQYL.showToast(`已读取 ${(result.sheet_names || []).length} 个 Sheet${selected ? `，当前选中：${selected}` : ""}`);
      }
    } catch (error) {
      renderSheetOptions([]);
      const message = error.message || String(error);
      if (showMessage) HQYL.showToast(message);
      else HQYL.appendLog(`读取指定 Sheet 失败：${message}；留空时将使用默认 Sheet`);
    } finally {
      loadingSheets = false;
    }
  }

  function storeNames() {
    const seen = new Set();
    return String($("lazadaBillStores").value || "").split(/\r?\n/).map((name) => name.trim()).filter((name) => {
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
    const list = $("lazadaBillOperatorList");
    list.replaceChildren();
    operatorNames.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      list.appendChild(option);
    });
    if (!$("lazadaBillOperatorName").value && operatorNames.length === 1) $("lazadaBillOperatorName").value = operatorNames[0];
  }

  function validateDates() {
    const start = $("lazadaBillStartDate").value;
    const end = $("lazadaBillEndDate").value;
    if (!start || !end) return "请选择账单开始和结束日期";
    if (start > end) return "开始日期不能晚于结束日期";
    if (end > todayIso()) return "结束日期不能晚于今天";
    return "";
  }

  function buildPayload(account) {
    return {
      account_id: account ? account.id : "",
      country: currentCountry(),
      sheet_name: $("lazadaBillSheetName").value,
      start_date: $("lazadaBillStartDate").value,
      end_date: $("lazadaBillEndDate").value,
      store_names: storeNames(),
      workbook_id: workbookIdValue(),
      dingtalk_operator_name: $("lazadaBillOperatorName").value.trim(),
      browser_window_mode: $("lazadaBillWindowMode").value,
      client_path: $("lazadaBillClientPath").value,
      screenshot_root: $("lazadaBillScreenshotDir").value.trim(),
      output_root: $("lazadaBillOutputDir").value,
    };
  }

  async function runCollection() {
    if (startingCollection) return;
    const payload = buildPayload(HQYL.activeAccount("ziniao"));
    const dateError = validateDates();
    const missing = !payload.country ? "请选择国家"
      : !payload.store_names.length ? "请输入至少一个店铺名称"
      : dateError || (!payload.dingtalk_operator_name ? "请填写钉钉操作人姓名"
      : !payload.workbook_id ? "请填写钉钉文档 ID"
      : !payload.screenshot_root ? "请选择截图保存位置"
      : !payload.output_root ? "请选择输出目录" : "");
    if (missing) { HQYL.showToast(missing); return; }
    if (!payload.account_id) { HQYL.openAccountDialog("ziniao", runCollection); return; }
    startingCollection = true;
    page.setRunning(true);
    try {
      if (!await savePreferences()) { page.setRunning(false); return; }
      await HQYL.startTask(() => {
        HQYL.appendLog(`准备采集 ${payload.country} ${payload.start_date} 至 ${payload.end_date} 的后台收支数据，指定 Sheet：${payload.sheet_name || defaultSheetName || "当前月份"}`);
        HQYL.appendLog(`店铺数量：${payload.store_names.length}，钉钉文档：${payload.workbook_id}`);
        HQYL.appendLog(`截图保存目录：${$("lazadaBillScreenshotHint").textContent.replace(/^本次截图保存目录：/, "")}`);
        return HQYL.api().start_lazada_bill_detail(payload);
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
      const result = await HQYL.api().get_lazada_bill_detail_info();
      if (!result.ok) throw new Error(result.error || "默认配置加载失败");
      const runtime = result.runtime || result;
      countryProfiles = Object.fromEntries((result.countries || []).map((item) => [item.code, item]));
      defaultWorkbookId = result.workbook_id || defaultWorkbookId;
      defaultSheetName = result.default_sheet_name || defaultSheetName;
      // 运行前就提示 F 列图片能不能写入：dws 缺失时不阻塞任务，但必须让用户看见。
      dwsAvailable = result.dws_available !== false;
      dwsHint = result.dws_hint || "未检测到 dws 命令，钉钉 F 列图片不会写入；截图仍会保存到本地。";
      renderDwsWarning();
      renderImageHint();
      const select = $("lazadaBillCountry");
      select.replaceChildren();
      (result.countries || []).forEach((item) => {
        const option = document.createElement("option");
        option.value = item.code;
        option.textContent = item.name;
        select.appendChild(option);
      });
      if (activeCountry && countryProfiles[activeCountry]) select.value = activeCountry;
      if (!select.value && select.options.length) select.value = select.options[0].value;
      activeCountry = select.value;
      const range = result.date_range || {};
      $("lazadaBillStartDate").value = range.start_date || "";
      $("lazadaBillEndDate").value = range.end_date || "";
      $("lazadaBillEndDate").max = todayIso();
      if (preferenceRevisions.client_path === revisions.client_path) {
        $("lazadaBillClientPath").value = runtime.client_path || "";
        savedPreferences.client_path = runtime.client_path || "";
        delete preferenceErrors.client_path;
      }
      if (preferenceRevisions.screenshot_root === revisions.screenshot_root) {
        $("lazadaBillScreenshotDir").value = runtime.screenshot_root || runtime.screenshot_dir || "";
        savedPreferences.screenshot_root = runtime.screenshot_root || runtime.screenshot_dir || "";
        delete preferenceErrors.screenshot_root;
      }
      if (preferenceRevisions.workbook_id === revisions.workbook_id) {
        defaultWorkbookId = result.workbook_id || defaultWorkbookId;
        savedPreferences.workbook_id = defaultWorkbookId;
        $("lazadaBillWorkbookId").value = defaultWorkbookId;
        delete preferenceErrors.workbook_id;
      }
      persistDefaultWorkbook();
      renderPreferenceStatus();
      renderImageHint();
      renderScreenshotHint();
      $("lazadaBillWindowMode").value = "normal";
      if (result.output_dir) $("lazadaBillOutputDir").value = result.output_dir;
      if (result.default_dingtalk_operator_name && !$("lazadaBillOperatorName").value) {
        $("lazadaBillOperatorName").value = result.default_dingtalk_operator_name;
      }
      renderOperators(result.operator_names || operatorNames);
      await loadSheets(false);
      if (showMessage) HQYL.showToast("已恢复默认配置：读取文档 Sheet 列表，账单区间默认为上月初至上月末");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function choosePath(method, targetId, failureMessage, withCurrentPath = false) {
    const revisionKey = Object.keys(PREFERENCE_TARGETS).find((key) => PREFERENCE_TARGETS[key].id === targetId);
    const revision = revisionKey ? preferenceRevisions[revisionKey] : 0;
    try {
      const result = withCurrentPath ? await HQYL.api()[method]($(targetId).value) : await HQYL.api()[method]();
      if (result.ok) {
        if (revisionKey && preferenceRevisions[revisionKey] !== revision) return;
        $(targetId).value = result.path || "";
        if (revisionKey) {
          preferenceRevisions[revisionKey] += 1;
          delete preferenceErrors[revisionKey];
          renderScreenshotHint();
          await savePreferences([revisionKey]);
        }
      } else if (!result.cancelled) throw new Error(result.error || failureMessage);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  function statusText(status) {
    return {
      success: "成功", written: "已写入", skipped: "已存在，跳过", skip: "已存在，跳过",
      unmatched_store: "未匹配店铺", unmatched: "未匹配店铺", failed: "失败",
      write_failed: "Sheet 写入失败", login_required: "需要登录", verification_required: "需要验证",
      browser_failed: "浏览器会话不可用",
    }[status] || status || "未知";
  }

  function money(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : String(value);
  }

  function renderResults(stores, range = {}) {
    const body = $("lazadaBillResultBody");
    body.replaceChildren();
    if (!stores.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 9;
      cell.textContent = "没有店铺结果";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    stores.forEach((store) => {
      const row = document.createElement("tr");
      const period = store.start_date && store.end_date ? `${store.start_date} ~ ${store.end_date}`
        : range.start_date && range.end_date ? `${range.start_date} ~ ${range.end_date}` : "—";
      [
        { text: store.store_name || store.requested_store_name || "—", title: store.screenshot ? `截图：${store.screenshot}` : "" },
        { text: store.country_name || store.country || "—" },
        { text: store.sheet_name || range.sheet_name || "—" },
        { text: period },
        { text: money(store.total_amount) },
        { text: money(store.revenue) },
        { text: money(store.deductions) },
        { text: statusText(store.status), title: store.image_cell ? `钉钉截图单元格：${store.image_cell}` : "" },
        { text: store.message || "—" },
      ].forEach(({ text, title }) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        if (title) cell.title = title;
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  const page = {
    key: "lazada_bill_detail",
    taskKey: "lazada_bill_detail",
    title: "Lazada 后台收支数据",
    async init(info) {
      $("lazadaBillEndDate").max = todayIso();
      $("lazadaBillOutputDir").value = (info.settings || {}).output_dir || "";
      renderOperators(((info.settings || {}).dingtalk || {}).operator_names || []);
      Object.entries(PREFERENCE_TARGETS).forEach(([key, target]) => {
        $(target.id).addEventListener("input", () => {
          preferenceRevisions[key] += 1;
          delete preferenceErrors[key];
          renderPreferenceStatus();
        });
        $(target.id).addEventListener("change", () => {
          if (key === "screenshot_root") renderScreenshotHint();
          if (key === "workbook_id") renderImageHint();
          savePreferences([key]);
          if (key === "workbook_id") loadSheets(false);
        });
      });
      $("lazadaBillCountry").addEventListener("change", () => {
        activeCountry = currentCountry();
        preferredSheetName = "";
        renderPreferenceStatus();
        renderScreenshotHint();
        loadSheets(false);
      });
      await refreshInfo(false);
      $("lazadaBillForm").addEventListener("submit", (event) => { event.preventDefault(); runCollection(); });
      $("lazadaBillStores").addEventListener("input", updateStoreCount);
      $("lazadaBillStartDate").addEventListener("change", renderScreenshotHint);
      $("lazadaBillEndDate").addEventListener("change", renderScreenshotHint);
      $("lazadaBillRefreshBtn").addEventListener("click", () => refreshInfo(true));
      $("lazadaBillRefreshSheetsBtn").addEventListener("click", () => loadSheets(true));
      $("lazadaBillChooseClientBtn").addEventListener("click", () => choosePath("choose_client_path", "lazadaBillClientPath", "选择客户端失败"));
      $("lazadaBillChooseScreenshotBtn").addEventListener("click", () => choosePath("choose_output_dir", "lazadaBillScreenshotDir", "选择截图保存位置失败", true));
      $("lazadaBillChooseOutputBtn").addEventListener("click", () => choosePath("choose_output_dir", "lazadaBillOutputDir", "选择输出目录失败", true));
      $("lazadaBillOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(resultOutputDir || $("lazadaBillOutputDir").value));
      updateStoreCount();
    },
    setRunning(running) {
      $("lazadaBillForm").querySelectorAll("input, select, textarea, button").forEach((element) => {
        if (element.id !== "lazadaBillOpenOutputBtn") element.disabled = running;
      });
    },
    resetResult() {
      $("matchedCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("failedCount").textContent = "失败 0";
      $("outputFile").textContent = "未生成";
      $("statusText").textContent = "等待任务启动";
      $("lazadaBillOpenOutputBtn").disabled = true;
      resultOutputDir = "";
      renderResults([]);
      updateStoreCount();
    },
    applyResult(result) {
      const rawStores = result.stores || result.results || result.store_results || [];
      const stores = Array.isArray(rawStores) ? rawStores : [];
      const successful = stores.filter((store) => ["success", "written"].includes(store.status)).length;
      const skipped = stores.filter((store) => ["skipped", "skip"].includes(store.status)).length;
      const failed = stores.filter((store) => ["failed", "unmatched_store", "write_failed", "login_required", "verification_required"].includes(store.status)).length;
      $("matchedCount").textContent = String(result.matched_store_count ?? stores.filter((store) => store.status !== "unmatched_store").length);
      $("recordCount").textContent = String(result.success_store_count ?? successful);
      $("failedCount").textContent = `失败 ${result.failed_store_count ?? failed} / 跳过 ${result.skipped_store_count ?? skipped}`;
      const imageNote = result.image_sync_note
        || `F 列图片：上传 ${result.image_uploaded_count ?? 0}，失败 ${result.image_failed_count ?? 0}，仅本地 ${result.image_local_only_count ?? 0}`;
      $("lazadaBillScreenshotHint").textContent = result.screenshot_dir
        ? `本次截图保存目录：${result.screenshot_dir}`
        : "本次截图保存目录：按「国家 + 账单区间」在截图根目录下自动创建";
      $("lazadaBillImageHint").textContent = result.workbook_id
        ? `钉钉截图写入：${result.workbook_id} 的 F 列（不会写入 A～E 列）；${imageNote}`
        : `钉钉截图写入同一文档的 F 列（不会写入 A～E 列）；${imageNote}`;
      resultOutputDir = result.output_dir || "";
      $("outputFile").textContent = result.output_file || result.message || (resultOutputDir ? "本地结果已保存" : "未生成本地结果");
      $("statusText").textContent = result.message || "任务结束";
      $("lazadaBillOpenOutputBtn").disabled = !resultOutputDir;
      renderResults(stores, { start_date: result.start_date, end_date: result.end_date, sheet_name: result.sheet_name });
    },
  };

  HQYL.boot(page);
})();
