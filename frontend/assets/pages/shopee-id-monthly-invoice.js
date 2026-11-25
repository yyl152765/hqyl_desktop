(() => {
  const $ = HQYL.$;
  const outputTool = "shopee_id_monthly_invoice";
  let profile = "", snapshot = null;
  let running = false, preparing = false, polling = false, revision = 0;
  let accountId = "", initialized = false;
  let outputSaveQueue = Promise.resolve();

  const busy = () => running || preparing;
  const activeId = () => HQYL.activeAccount("ziniao")?.id || "";
  const accountPayload = () => ({
    account_id: activeId(),
    client_path: $("clientPath").value.trim(),
    webdriver_path: $("driverPath").value.trim(),
  });

  function ensureAccount(continuation) {
    if (activeId()) return true;
    HQYL.openAccountDialog("ziniao", continuation);
    return false;
  }

  async function call(method, payload) {
    const result = await HQYL.api()[method](payload);
    if (!result.ok) throw new Error(result.error || "操作失败");
    return result;
  }

  function validation() {
    const rawNames = $("storeNames").value;
    const month = $("invoiceMonth").value.trim();
    const outDir = $("outputDir").value.trim();
    if (!month || !rawNames.trim() || !outDir) {
      return "请选择账单月份、填写店铺名并设置输出目录。";
    }
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
      return "账单月份格式必须为 YYYY-MM。";
    }
    const lines = rawNames.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (!lines.length) {
      return "请至少输入一个 Shopee 印尼店铺名或店号简称。";
    }
    if (new Set(lines.map(item => item.toLowerCase())).size !== lines.length) {
      return "店铺名称不能重复，请检查输入。";
    }
    return "";
  }

  function controls() {
    const disabled = busy();
    ["invoiceMonth", "storeNames", "outputDir", "chooseOutputBtn", "chooseClientBtn", "chooseDriverBtn"].forEach(id => {
      $(id).disabled = disabled;
    });
    const hasBasicInputs = Boolean($("invoiceMonth").value.trim() && $("storeNames").value.trim());
    $("previewBtn").disabled = disabled || !hasBasicInputs;
    $("startBtn").disabled = disabled || Boolean(validation());
    $("retryBtn").disabled = disabled || !activeId() || !snapshot?.can_retry || (Boolean(profile) && snapshot.profile !== profile);
    $("openDirectoryBtn").disabled = !snapshot?.output_dir;
  }

  function showIssue(message) {
    $("startIssues").hidden = !message;
    $("startIssues").textContent = message || "";
  }

  function showPreview(message) {
    $("previewBox").hidden = !message;
    $("previewBox").textContent = message || "";
  }

  function invalidate() {
    revision++;
    showIssue("");
    showPreview("");
    controls();
  }

  function saveOutputDir() {
    const output_dir = $("outputDir").value.trim();
    if (!output_dir || typeof HQYL.api().save_scoped_output_dirs !== "function") {
      return Promise.resolve();
    }
    const operation = outputSaveQueue
      .catch(() => {})
      .then(() => call("save_scoped_output_dirs", { tool: outputTool, output_dir }));
    outputSaveQueue = operation;
    return operation;
  }

  async function prepare(action) {
    if (busy()) return;
    preparing = true;
    controls();
    try {
      await action();
    } catch (error) {
      showIssue(error.message || String(error));
      HQYL.showToast(error.message || String(error));
    } finally {
      preparing = false;
      controls();
    }
  }

  async function openPath(path) {
    if (!path) return;
    try {
      await call("open_path", path);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  function render(result) {
    if (!result || typeof result !== "object") return;
    snapshot = result;
    $("storeCount").textContent = String(result.store_count || 0);
    $("completedCount").textContent = String(result.completed_count || 0);
    $("noDataCount").textContent = `无税票 ${result.no_data_count || 0} · 失败 ${result.failed_count || 0}`;
    $("recordCount").textContent = Number(result.file_count || 0).toLocaleString();
    $("invoiceBreakdown").textContent = `Master ${result.master_invoice_count || 0} · Tax ${result.tax_invoice_count || 0}`;
    $("outputFile").textContent = result.is_complete ? "下载已完成" : (result.file_count ? "部分已保存" : "未完成");
    $("batchLabel").textContent = result.run_id || "尚无批次";
    $("resultMessage").textContent =
      result.status === "running"
        ? "正在逐店下载税票 PDF，下方展示各店铺最新状态。"
        : result.status === "interrupted"
          ? "上次批次已中断，已下载的 PDF 已保留，可点击重试继续未完成店铺。"
          : (result.completion_message || "批次已结束");

    if (!running && result.status !== "running") {
      $("taskBadge").className = `status-pill ${result.is_complete ? "success" : "failed"}`;
      $("taskBadge").textContent = result.is_complete ? "已完成" : "结果不完整";
      $("statusText").textContent = result.is_complete ? "全部税票已校验保存" : "可处理失败项后重试";
    }

    const labels = {
      pending: "等待",
      running: "运行中",
      success: "成功",
      no_data: "无税票 (No receipt found)",
      failed: "失败",
    };
    const body = $("storeResults");
    body.replaceChildren();
    (result.stores || []).forEach(store => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      nameCell.textContent = store.store_name || "—";
      const sub = document.createElement("small");
      const req = store.requested_name && store.requested_name !== store.store_name ? `输入：${store.requested_name} · ` : "";
      sub.textContent = `${req}ID ${store.store_id}`;
      nameCell.appendChild(sub);
      row.appendChild(nameCell);

      const statusCell = document.createElement("td");
      statusCell.textContent = labels[store.status] || store.status || "—";
      row.appendChild(statusCell);

      const rowCountCell = document.createElement("td");
      rowCountCell.textContent = store.row_count == null ? "—" : String(store.row_count);
      row.appendChild(rowCountCell);

      const masterCell = document.createElement("td");
      masterCell.textContent = `${store.master_invoice_count || 0} 份`;
      row.appendChild(masterCell);

      const taxCell = document.createElement("td");
      taxCell.textContent = `${store.tax_invoice_count || 0} 份`;
      row.appendChild(taxCell);

      const folderCell = document.createElement("td");
      if (store.store_output_dir) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "secondary compact-btn";
        btn.textContent = "打开店铺目录";
        btn.addEventListener("click", () => openPath(store.store_output_dir));
        folderCell.appendChild(btn);
      } else {
        folderCell.textContent = "—";
      }
      row.appendChild(folderCell);

      const noteCell = document.createElement("td");
      if (store.error) {
        noteCell.textContent = store.error;
      } else if (store.status === "no_data") {
        noteCell.textContent = `${result.month || ""} 页面确认 No receipt found`;
      } else if (store.status === "success") {
        noteCell.textContent = `已校验 ${store.file_count || 0} 份 PDF (SHA-256)`;
      } else {
        noteCell.textContent = "—";
      }
      row.appendChild(noteCell);

      body.appendChild(row);
    });
    if (!body.childNodes.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.textContent = "尚未运行";
      row.appendChild(cell);
      body.appendChild(row);
    }
    controls();
  }

  async function previewStores() {
    if (busy()) return;
    if (!ensureAccount(previewStores)) return;
    await prepare(async () => {
      showIssue("");
      showPreview("");
      const payload = {
        ...accountPayload(),
        month: $("invoiceMonth").value.trim(),
        store_names: $("storeNames").value,
      };
      const version = revision;
      const result = await call("preview_shopee_id_monthly_invoice_stores", payload);
      if (version !== revision || payload.account_id !== activeId()) return;
      if (result.issues && result.issues.length) {
        const issueText = result.issues.map(item => `${item.name}：${item.error}`).join("\n");
        showIssue(issueText);
      }
      if (result.stores && result.stores.length) {
        const matchedText = result.stores
          .map(item => `${item.requested_name || item.store_name} → ${item.store_name} (ID ${item.store_id})`)
          .join("；");
        showPreview(`已匹配 ${result.stores.length} 家 Shopee 印尼店铺：${matchedText}`);
      }
    });
  }

  async function updateProgress(path) {
    if (polling || !path) return;
    polling = true;
    const version = revision;
    try {
      const request = { ...accountPayload(), manifest_path: path };
      const result = await call("get_shopee_id_monthly_invoice_progress", request);
      if (!result.result) return;
      if (version === revision && request.account_id === activeId() && (!profile || result.result.profile === profile)) {
        render(result.result);
      }
    } catch (error) {
      if (version === revision) {
        $("resultMessage").textContent = `读取进度失败：${error.message}`;
      }
    } finally {
      polling = false;
    }
  }

  async function start(retry = false) {
    if (busy()) return;
    if (!retry) {
      const err = validation();
      if (err) {
        showIssue(err);
        return;
      }
    }
    if (!ensureAccount(() => start(retry))) return;
    const payload = {
      ...accountPayload(),
      ...(retry
        ? { manifest_path: snapshot?.manifest_path }
        : {
            month: $("invoiceMonth").value.trim(),
            store_names: $("storeNames").value.trim(),
            output_dir: $("outputDir").value.trim(),
          }),
    };
    const version = ++revision;
    showIssue("");
    $("connectionNotice").textContent = retry
      ? "正在恢复本账号的税票下载批次…"
      : "正在核对紫鸟账号下的 Shopee 印尼店铺并启动下载…";
    await HQYL.startTask(
      async () => {
        try {
          if (!retry) await saveOutputDir();
          const task = await call(
            retry ? "retry_shopee_id_monthly_invoice" : "start_shopee_id_monthly_invoice",
            payload
          );
          if (version === revision && payload.account_id === activeId()) {
            profile = task.context?.profile || profile;
            $("connectionNotice").textContent = "任务已启动，按名单顺序逐店下载 Master Invoice 与 Tax Invoice。";
          }
          return task;
        } catch (error) {
          if (version === revision && payload.account_id === activeId()) {
            showIssue(error.message || String(error));
            $("connectionNotice").textContent = "启动未完成，请按提示处理后重新开始。";
          }
          throw error;
        }
      },
      () => version === revision && payload.account_id === activeId()
    );
    controls();
  }

  async function loadInfo() {
    const request = accountPayload();
    const version = revision;
    try {
      const result = await call("get_shopee_id_monthly_invoice_info", request);
      if (version !== revision || request.account_id !== activeId()) return;
      profile = result.profile || "";
      if (!$("invoiceMonth").value) $("invoiceMonth").value = result.default_month || "";
      $("invoiceMonth").max = result.default_month || "";
      if (!$("clientPath").value) $("clientPath").value = result.client_path || "";
      if (!$("driverPath").value) $("driverPath").value = result.webdriver_path || "";
      if (!$("outputDir").value.trim()) $("outputDir").value = result.output_dir || "";
      if (result.latest) render(result.latest);
    } catch (error) {
      if (version === revision) $("connectionNotice").textContent = error.message || String(error);
    }
    controls();
  }

  const page = {
    key: "shopee_id_monthly_invoice",
    taskKey: "shopee_id_monthly_invoice",
    title: "Shopee 印尼月度税票下载",
    taskContextFilter: context =>
      Boolean(activeId()) &&
      context.account_id === activeId() &&
      (!profile || !context.profile || context.profile === profile),
    async init() {
      accountId = activeId();
      $("exportForm").addEventListener("submit", event => {
        event.preventDefault();
        void start(false);
      });
      ["invoiceMonth", "storeNames"].forEach(id => {
        $(id).addEventListener("input", invalidate);
      });
      $("outputDir").addEventListener("input", () => {
        showIssue("");
        controls();
      });
      $("outputDir").addEventListener("change", () => {
        void saveOutputDir().catch(error => showIssue(error.message || String(error)));
      });
      $("chooseOutputBtn").addEventListener("click", () =>
        prepare(async () => {
          await outputSaveQueue.catch(() => {});
          const result = await HQYL.api().choose_scoped_output_dir({
            tool: outputTool,
            kind: "output",
            current_dir: $("outputDir").value.trim(),
          });
          if (result.ok) {
            $("outputDir").value = result.path;
            controls();
          } else if (!result.cancelled) {
            throw new Error(result.error);
          }
        })
      );
      [
        ["chooseClientBtn", "clientPath", "choose_client_path"],
        ["chooseDriverBtn", "driverPath", "choose_driver_path"],
      ].forEach(([button, input, method]) => {
        $(button).addEventListener("click", () =>
          prepare(async () => {
            const result = await HQYL.api()[method]();
            if (result.ok) {
              $(input).value = result.path;
              invalidate();
            } else if (!result.cancelled) {
              throw new Error(result.error);
            }
          })
        );
      });
      $("previewBtn").addEventListener("click", () => void previewStores());
      $("openDirectoryBtn").addEventListener("click", () => openPath(snapshot?.output_dir));
      $("retryBtn").addEventListener("click", () => void start(true));
      initialized = true;
      await loadInfo();
      controls();
    },
    onAccountsChanged() {
      if (!initialized || accountId === activeId()) return;
      accountId = activeId();
      profile = "";
      running = false;
      invalidate();
      page.resetResult();
      $("logBox").textContent = "";
      $("taskBadge").className = "status-pill idle";
      $("taskBadge").textContent = "待运行";
      $("statusText").textContent = "请填写本账号的店铺名";
      $("connectionNotice").textContent = "账号已切换，开始下载时将使用当前紫鸟账号匹配 Shopee 印尼店铺。";
      $("resultMessage").textContent = "所选账号尚未加载批次。";
      void loadInfo();
    },
    setRunning(value) {
      running = value;
      controls();
    },
    resetResult() {
      snapshot = null;
      $("storeResults").replaceChildren();
      $("storeCount").textContent = "0";
      $("completedCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("noDataCount").textContent = "无税票 0 · 失败 0";
      $("invoiceBreakdown").textContent = "Master 0 · Tax 0";
      $("outputFile").textContent = "未生成";
      $("batchLabel").textContent = "准备新任务";
      $("resultMessage").textContent = "正在启动，请稍候…";
      controls();
    },
    onTaskUpdate(task) {
      if (task.context?.account_id === activeId() && task.context?.manifest_path) {
        void updateProgress(task.context.manifest_path);
      }
    },
    applyResult(result) {
      if (result && result.manifest_path && (!profile || !result.profile || result.profile === profile)) {
        revision++;
        render(result);
      }
    },
  };
  HQYL.boot(page);
})();
