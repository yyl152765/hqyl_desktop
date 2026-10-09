(() => {
  const $ = HQYL.$;
  const REPORT_TYPES = ["income", "ads", "affiliate", "ads_credit"];
  const STAGE3_DOC_PATH = "D:\\hqyl_project\\hqyl_desktop\\docs\\越南收支报表核对第三阶段_最终文件处理开发方案.md";
  
  let outputDir = "";
  let runDir = "";
  let manifestPath = "";
  let currentManifest = null;
  let taskRunning = false;
  let activeTask = null;
  let currentDashboard = null;
  let userPinnedTab = false;
  let userPinnedStep = false;
  let lastDerivedKind = "";
  let storeRows = [];
  let storeTotals = {};
  let storeSort = { key: "", direction: "desc" };
  let anomalyPage = 1;
  const anomalyPageSize = 10;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  const moneyFmt = new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  // 金额格式化：区分“真正的 0”和“缺失值”。缺失返回 —，其余带千分位与两位小数。
  function formatMoney(value) {
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return moneyFmt.format(n);
  }

  // 利润率：API 以小数返回（0.125 -> 12.50%）。缺失返回 —。
  function formatPercent(value) {
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return `${(n * 100).toFixed(2)}%`;
  }

  function isNegative(value) {
    const n = Number(value);
    return Number.isFinite(n) && n < 0;
  }

  function setMoneyKpi(id, value) {
    const el = $(id);
    if (!el) return;
    el.textContent = formatMoney(value);
    el.classList.toggle("is-negative", isNegative(value));
  }

  const REPORT_STATUS_LABELS = {
    pending: "待运行",
    running: "进行中",
    success: "已完成",
    no_data: "0 条（已完成）",
    failed: "失败",
    passed: "校验通过",
  };

  function reportStatusText(status) {
    return REPORT_STATUS_LABELS[status] || status || "待运行";
  }

  function setPageBadge(kind, text) {
    const badge = $("taskBadge");
    if (badge) {
      badge.className = `status-pill ${kind}`;
      badge.textContent = text;
    }
  }

  function parseStores() {
    const lines = $("storeInput").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const stores = [];
    const seen = new Set();
    for (const line of lines) {
      const name = String(line || "").trim();
      if (!name) continue;
      const identity = name.toLocaleLowerCase();
      if (seen.has(identity)) continue;
      seen.add(identity);
      stores.push({ name });
    }
    return stores;
  }

  function pendingReports() {
    return Object.fromEntries(REPORT_TYPES.map((key) => [key, { status: "pending" }]));
  }

  function draftManifest(stores) {
    return {
      period: $("collectionPeriod").value,
      status: "draft",
      stores: stores.map((store) => ({
        store_name: store.name,
        store_type: store.name.includes("仓发") ? "warehouse" : "normal",
        reports: pendingReports(),
      })),
    };
  }

  function renderDraftMatrix(manifest) {
    const stores = Array.isArray(manifest?.stores) ? manifest.stores : [];
    $("collectionMatrixEmpty").hidden = stores.length > 0;
    $("collectionTableWrap").hidden = stores.length === 0;
    
    const statusChip = (report) => {
      const status = report?.status || "pending";
      const labels = { pending: "待采集", running: "采集中", success: "已完成", failed: "失败", no_data: "无数据" };
      return `<span class="collection-state ${escapeHtml(status)}"><i></i>${escapeHtml(labels[status] || status)}</span>`;
    };

    $("collectionTableBody").innerHTML = stores.map((store) => `
      <tr>
        <td><strong>${escapeHtml(store.store_name)}</strong></td>
        <td colspan="5" style="text-align: center; color: #64748b;">批次未创建，无法查看明细数据</td>
        <td>${statusChip(store.reports?.income)}</td>
      </tr>`).join("");
  }

  // Anomalies rendering
  function renderAnomaliesList(exceptions) {
    const listEl = $("anomalyList");
    if (!exceptions || exceptions.length === 0) {
      listEl.innerHTML = '<div class="text-muted text-center" style="padding: 3rem 0;">没有发现任何异常数据项。</div>';
      return;
    }

    listEl.innerHTML = exceptions.map((e) => {
      const sevClass = e.severity === "blocking" ? "blocking" : "attention";
      const sevText = e.severity === "blocking" ? "阻断" : "提示";
      return `
        <div class="anomaly-card severity-${escapeHtml(e.severity)}">
          <div class="anomaly-header">
            <span class="anomaly-tag ${sevClass}">${escapeHtml(sevText)}</span>
            <strong style="color: #f1f5f9;">${escapeHtml(e.title)}</strong>
            <span style="font-size: 0.8rem; color: #64748b;">${escapeHtml(e.category)}</span>
          </div>
          <div style="font-size: 0.85rem; color: #cbd5e1; margin-top: 0.25rem;">
            ${escapeHtml(e.message)}
          </div>
          <div style="font-size: 0.8rem; color: #94a3b8; display: flex; justify-content: space-between; margin-top: 0.5rem; border-top: 1px solid rgba(255,255,255,0.03); padding-top: 0.4rem;">
            <span>建议: ${escapeHtml(e.suggestion)}</span>
            ${e.store_name ? `<span>店铺: ${escapeHtml(e.store_name)}</span>` : ""}
          </div>
        </div>`;
    }).join("");
  }

  function isLiveTask(task) {
    return task?.status === "running" || task?.status === "pending";
  }

  function deriveVietnamReportState(dashboard, task) {
    const state = dashboard?.state || (manifestPath ? "draft" : "empty");
    const definitions = {
      empty: { kind: "empty", label: "待创建", activeTab: "dataPrep", activeStep: 1, actionId: "createRunBtn", hint: "先创建本月报表批次。" },
      draft: { kind: "draft", label: "数据准备中", activeTab: "dataPrep", activeStep: 2, actionId: "startSubsidyBtn", hint: "继续采集紫鸟与马帮原始数据。" },
      running: { kind: "running", label: "运行中", activeTab: "dataPrep", activeStep: 2, actionId: "toggleLogDrawerBtn", hint: "任务正在处理，可展开日志查看进度。" },
      interrupted: { kind: "needs_attention", label: "上次运行已中断", activeTab: "dataPrep", activeStep: 2, actionId: "startSubsidyBtn", hint: "应用已重启，原任务不会继续运行，请从中断步骤重试。" },
      generating: { kind: "generating", label: "正在生成", activeTab: "dataPrep", activeStep: 4, actionId: "toggleLogDrawerBtn", hint: "正在生成报表，请等待任务完成。" },
      blocked: { kind: "blocked", label: "已阻断", activeTab: "anomalies", activeStep: 3, actionId: "startMabangIncomeBtn", hint: "请按异常提示修复或重试后再生成。" },
      needs_attention: { kind: "needs_attention", label: "需要处理", activeTab: "dataPrep", activeStep: 4, actionId: "profitRateInput", hint: "补齐退款店铺的上月利润率。" },
      ready_to_generate: { kind: "ready_to_generate", label: "可以生成", activeTab: "dataPrep", activeStep: 4, actionId: "startFinalProcessBtn", hint: "数据已齐全，可以生成收支报表。" },
      ready: { kind: "ready", label: "已生成", activeTab: "overview", activeStep: 4, actionId: "openOutputFileBtn", hint: "报表已生成，可打开 Excel 查看。" },
      ready_with_issues: { kind: "ready_with_issues", label: "已生成 · 有异常", activeTab: "overview", activeStep: 4, actionId: "openOutputFileBtn", hint: "报表已生成，请同时复核未匹配清单。" },
      final_file_missing: { kind: "failed", label: "文件已不存在", activeTab: "overview", activeStep: 4, actionId: "startFinalProcessBtn", hint: "报表文件已被移动或删除，请重新生成。" },
      failed: { kind: "failed", label: "生成失败", activeTab: "overview", activeStep: 4, actionId: "startFinalProcessBtn", hint: "请查看日志后重新生成报表。" },
    };
    if (isLiveTask(task)) return { ...definitions.running, activeStep: task.tool === "vietnam_final_reconciliation" ? 4 : task.tool?.includes("mabang") ? 3 : 2 };
    if (dashboard?.interrupted) {
      const runningItems = collectRunningItems(currentManifest || {});
      if (runningItems.includes("final")) return { ...definitions.interrupted, activeStep: 4, actionId: "startFinalProcessBtn" };
      if (runningItems.some((item) => item.startsWith("mabang:refund"))) return { ...definitions.interrupted, activeStep: 4, actionId: "startMabangRefundBtn" };
      if (runningItems.some((item) => item.startsWith("mabang:"))) return { ...definitions.interrupted, activeStep: 3, actionId: "startMabangIncomeBtn" };
      return definitions.interrupted;
    }
    return definitions[state] || definitions.draft;
  }

  function activateTab(name, automatic = false) {
    if (automatic && userPinnedTab) return;
    const mapping = { overview: ["tabOverview", "contentOverview"], anomalies: ["tabAnomalies", "contentAnomalies"], dataPrep: ["tabDataPrep", "contentDataPrep"] };
    Object.values(mapping).forEach(([buttonId, contentId]) => {
      $(buttonId).classList.toggle("active", buttonId === mapping[name][0]);
      $(contentId).classList.toggle("active", contentId === mapping[name][1]);
    });
  }

  function activateStep(step, automatic = false) {
    if (automatic && userPinnedStep) return;
    document.querySelectorAll(".step-pane").forEach((pane, index) => pane.classList.toggle("active", index + 1 === step));
    document.querySelectorAll(".wizard-step").forEach((item, index) => {
      item.classList.toggle("active", index + 1 === step);
      item.classList.toggle("completed", index + 1 < step);
    });
  }

  function applyDerivedState(dashboard, reason = "refresh") {
    const derived = deriveVietnamReportState(dashboard, activeTask);
    const shouldAutoSelect = reason === "initial" || reason === "batch" || (lastDerivedKind && lastDerivedKind !== derived.kind);
    if (shouldAutoSelect) {
      activateTab(derived.activeTab, true);
      activateStep(derived.activeStep, true);
    }
    lastDerivedKind = derived.kind;
    const title = $("primaryGuidanceTitle");
    const hint = $("primaryGuidanceHint");
    const action = $("primaryGuidanceAction");
    title.textContent = derived.label;
    hint.textContent = derived.hint;
    action.textContent = derived.actionId === "openOutputFileBtn" ? "打开 Excel 报表" : derived.actionId === "startFinalProcessBtn" ? "生成收支报表" : derived.actionId === "startMabangIncomeBtn" ? "重新导出并校验" : derived.actionId === "createRunBtn" ? "创建报表批次" : derived.actionId === "toggleLogDrawerBtn" ? "查看处理进度" : "前往处理";
    action.hidden = !derived.actionId;
    action.onclick = () => {
      const target = $(derived.actionId);
      if (!target) return;
      if (derived.actionId === "profitRateInput") target.focus();
      else target.click();
    };
  }

  function renderStoreTable() {
    const query = $("storeSearchInput").value.trim().toLocaleLowerCase();
    const status = $("storeStatusFilter").value;
    let rows = storeRows.filter((row) => (!query || String(row.store_name || "").toLocaleLowerCase().includes(query)) && (status === "all" || row.status === status));
    rows.sort((a, b) => {
      if (!storeSort.key) {
        const exceptionDiff = Number(b.status === "exception") - Number(a.status === "exception");
        return exceptionDiff || Number(b.profit || 0) - Number(a.profit || 0);
      }
      return (Number(a[storeSort.key] || 0) - Number(b[storeSort.key] || 0)) * (storeSort.direction === "asc" ? 1 : -1);
    });
    $("collectionMatrixEmpty").hidden = storeRows.length > 0;
    $("collectionTableWrap").hidden = storeRows.length === 0 || rows.length === 0;
    $("storeFilterEmpty").hidden = storeRows.length === 0 || rows.length > 0;
    const moneyCell = (value) => `<td class="${isNegative(value) ? "is-negative" : ""}">${escapeHtml(formatMoney(value))}</td>`;
    $("collectionTableBody").innerHTML = rows.map((s) => `
      <tr class="${s.status === "exception" ? "has-exception" : ""}">
        <td><strong>${escapeHtml(s.store_name)}</strong></td><td>${escapeHtml(s.order_count ?? "—")}</td>
        ${moneyCell(s.adjusted_receivable)}${moneyCell(s.total_expense)}${moneyCell(s.actual_refund)}${moneyCell(s.final_ads)}${moneyCell(s.actual_affiliate)}${moneyCell(s.profit)}
        <td class="${isNegative(s.profit_rate) ? "is-negative" : ""}">${escapeHtml(formatPercent(s.profit_rate))}</td>
        <td>${s.status === "exception" ? "有异常" : "正常"}</td>
      </tr>`).join("");
    $("collectionTableFoot").innerHTML = rows.length ? `<tr><td>总计</td><td>${escapeHtml(storeTotals.order_count ?? "—")}</td>${moneyCell(storeTotals.adjusted_receivable)}${moneyCell(storeTotals.total_expense)}${moneyCell(storeTotals.actual_refund)}${moneyCell(storeTotals.final_ads)}${moneyCell(storeTotals.actual_affiliate)}${moneyCell(storeTotals.profit)}<td>${escapeHtml(formatPercent(storeTotals.profit_rate))}</td><td>—</td></tr>` : "";
  }

  function renderResultFiles(dashboard) {
    const files = dashboard.files || {};
    const generated = ["ready", "ready_with_issues", "final_file_missing"].includes(dashboard.state);
    $("resultFilesCard").hidden = !generated;
    if (!generated) return;
    const missing = dashboard.state === "final_file_missing" || files.output_exists === false;
    $("missingOutputAlert").hidden = !missing;
    $("resultFileName").textContent = files.output_file ? files.output_file.split(/[\\/]/).pop() : "Excel 文件不存在";
    $("resultFileMeta").textContent = dashboard.generated_at ? `生成时间：${dashboard.generated_at}` : "报表生成结果与复核入口。";
    const summary = dashboard.summary || {};
    $("resultFileStats").textContent = `平均汇率 ${summary.average_exchange_rate ?? "—"} · 明细 ${summary.mabang_detail_row_count ?? "—"} 行 · 退款 ${summary.refund_row_count ?? "—"} 行 · 未匹配 ${dashboard.exception_summary?.total_count ?? 0} 项`;
    $("openOutputFileBtn").disabled = missing;
    $("openUnmatchedFileBtn").hidden = !files.unmatched_exists;
    $("openProcessingLogBtn").disabled = !files.processing_log;
    $("openResultDirBtn").disabled = !files.run_dir;
  }

  async function loadDashboard(path) {
    if (!path) return;
    manifestPath = path;
    const res = await HQYL.api().get_vietnam_report_dashboard(path);
    if (!res.ok) {
      HQYL.showToast(res.error || "加载报表概览失败");
      return;
    }
    
    currentDashboard = res;
    runDir = res.run_dir || "";
    currentManifest = res.config;

    // Update KPI metrics（未生成时 totals 为空对象，formatMoney 会显示 —）
    const totals = res.totals || {};
    setMoneyKpi("kpiReceivable", totals.adjusted_receivable);
    setMoneyKpi("kpiExpense", totals.total_expense);
    setMoneyKpi("kpiRefund", totals.actual_refund);
    setMoneyKpi("kpiAds", totals.final_ads);
    setMoneyKpi("kpiProfit", totals.profit);

    const rateEl = $("kpiProfitRate");
    rateEl.textContent = formatPercent(totals.profit_rate);
    rateEl.classList.toggle("is-negative", isNegative(totals.profit_rate));

    storeRows = res.stores || [];
    storeTotals = res.totals || {};
    renderStoreTable();
    renderResultFiles(res);
    
    // State badge mapping
    const stateLabels = {
      empty: "无数据",
      draft: "草稿",
      running: "运行中",
      needs_attention: "需关注",
      blocked: "已阻断",
      ready_to_generate: "就绪可生成",
      generating: "生成中",
      ready: "已生成",
      ready_with_issues: "已生成 · 有异常",
      final_file_missing: "文件已不存在",
      failed: "失败"
    };
    const badgeKinds = {
      empty: "idle",
      draft: "idle",
      running: "running",
      needs_attention: "warning",
      blocked: "failed",
      ready_to_generate: "warning",
      generating: "running",
      ready: "success",
      ready_with_issues: "warning",
      final_file_missing: "failed",
      failed: "failed"
    };
    setPageBadge(badgeKinds[res.state] || "idle", stateLabels[res.state] || res.state);
    
    // Update step 1 Inputs（月份回填即可；输出目录保持用户配置的“输出根目录”，不要写成批次运行目录）
    $("collectionPeriod").value = res.period || "";

    // 预填利润率表：从需要利润率的退款店铺 + 已保存配置生成
    const savedRates = (res.config && res.config.profit_rates) || {};
    if (res.required_profit_rate_stores && res.required_profit_rate_stores.length > 0) {
      const lines = res.required_profit_rate_stores.map((s) => {
        const rate = savedRates[s] !== undefined ? `${(parseFloat(savedRates[s]) * 100).toFixed(2)}%` : "";
        return `${s}=${rate}`;
      });
      $("profitRateInput").value = lines.join("\n");
    }

    // Set interactive buttons disabled states
    const isGenerating = (res.state === "generating");
    $("startSubsidyBtn").disabled = taskRunning || isGenerating;
    $("startSourcesBtn").disabled = taskRunning || isGenerating;
    $("startMabangIncomeBtn").disabled = taskRunning || isGenerating;
    $("startMabangRefundBtn").disabled = taskRunning || isGenerating;
    $("startFinalProcessBtn").disabled = taskRunning || isGenerating;
    
    $("openRunDirBtn").disabled = !runDir;
    $("openManifestBtn").disabled = !manifestPath;
    
    // Handle step state highlights
    // Enable/disable buttons based on derived states
    if (res.state === "ready" || res.state === "ready_with_issues" || res.state === "ready_to_generate" || res.state === "needs_attention") {
      $("startFinalProcessBtn").disabled = taskRunning;
    }
    
    // Populate anomalies Tab Badge count
    const totalExceptions = res.exceptions ? res.exceptions.length : 0;
    $("anomalyTabBadge").textContent = String(totalExceptions);
    $("anomalyTabBadge").style.display = totalExceptions > 0 ? "inline" : "none";
    
    // 拉取完整 manifest 以填充“数据准备”向导中各步骤的真实状态
    try {
      const manifestRes = await HQYL.api().get_vietnam_collection_run(manifestPath);
      if (manifestRes && manifestRes.ok && manifestRes.manifest) {
        currentManifest = manifestRes.manifest;
        renderManifestSteps(manifestRes.manifest);
        const hasStaleRunning = collectRunningItems(manifestRes.manifest).length > 0 && !isLiveTask(activeTask);
        if (hasStaleRunning) {
          res.interrupted = true;
          setPageBadge("warning", "需要处理");
          markInterruptedSteps(manifestRes.manifest);
        }
      }
    } catch (err) {
      // 旧版 Bridge 缺少该接口时静默降级，不阻断概览渲染
    }

    // Load paginated list of anomalies
    await loadAnomalies();
    await loadHistoryRuns();
    updateBlockingAndHints(res);
    applyDerivedState(res, lastDerivedKind ? "refresh" : "initial");
  }

  function collectRunningItems(manifest) {
    const items = [];
    (manifest?.stores || []).forEach((store) => Object.entries(store.reports || {}).forEach(([key, report]) => {
      if (report?.status === "running") items.push(`ziniao:${key}`);
    }));
    Object.entries(manifest?.mabang?.reports || {}).forEach(([key, report]) => {
      if (report?.status === "running") items.push(`mabang:${key}`);
    });
    if (manifest?.final?.status === "running") items.push("final");
    return items;
  }

  function markInterruptedSteps(manifest) {
    const running = collectRunningItems(manifest);
    if (running.some((item) => item.startsWith("mabang:"))) {
      $("mabangPhaseStatus").className = "collection-state attention";
      $("mabangPhaseStatus").innerHTML = "<i></i>上次运行已中断，可重试";
    }
    if (running.includes("final")) {
      $("finalPhaseStatus").className = "collection-state attention";
      $("finalPhaseStatus").innerHTML = "<i></i>上次运行已中断，可重试";
    }
  }

  function updateBlockingAndHints(dashboard) {
    const validationFailed = currentManifest?.mabang?.validation?.status === "failed";
    $("validationBlockedBanner").hidden = !validationFailed;
    $("startMabangIncomeBtn").disabled = taskRunning;
    $("mabangIncomeHint").textContent = taskRunning ? "当前已有任务运行中" : "";
    const progress = dashboard.data_progress || {};
    const remaining = Math.max(Number(progress.total || 0) - Number(progress.completed || 0), 0);
    const missingRates = (dashboard.required_profit_rate_stores || []).filter((store) => dashboard.config?.profit_rates?.[store] === undefined);
    $("startMabangRefundBtn").disabled = taskRunning || validationFailed;
    $("mabangRefundHint").textContent = validationFailed ? "已阻断：马帮收支校验未通过，请先重新导出" : taskRunning ? "当前已有任务运行中" : "";
    const canGenerate = ["ready_to_generate", "ready", "ready_with_issues", "final_file_missing", "failed"].includes(dashboard.state);
    $("startFinalProcessBtn").disabled = taskRunning || validationFailed || (!canGenerate && missingRates.length === 0);
    if (validationFailed) $("finalProcessHint").textContent = "已阻断：马帮收支校验未通过，请先到「异常核对」处理";
    else if (missingRates.length) $("finalProcessHint").textContent = `还缺 ${missingRates.length} 家店铺利润率：${missingRates.slice(0, 3).join("、")}${missingRates.length > 3 ? "…" : ""}`;
    else if (!canGenerate && remaining) $("finalProcessHint").textContent = `还需完成 ${remaining} 项数据准备（${progress.completed || 0}/${progress.total || 0}）`;
    else $("finalProcessHint").textContent = "";
    $("stepPane4").classList.toggle("is-locked", validationFailed);
  }

  // 用 manifest 中的真实报表状态填充“数据准备”向导（此前这些状态框恒为“待运行”死值）
  function renderManifestSteps(manifest) {
    const mabang = manifest.mabang || {};
    const reports = mabang.reports || {};
    const validation = mabang.validation || {};
    const stores = Array.isArray(manifest.stores) ? manifest.stores : [];

    const setText = (id, text) => {
      const el = $(id);
      if (el) el.textContent = text;
    };
    const reportStatus = (key) => reportStatusText((reports[key] || {}).status);
    const reportRows = (key) => {
      const rc = (reports[key] || {}).row_count;
      return rc === null || rc === undefined ? "" : `${rc} 行`;
    };

    // 步骤三：马帮收支明细/汇总/校验
    setText("mabangShopCount", stores.length ? String(stores.length) : "—");
    setText("mabangDetailStatus", `${reportStatus("income_detail")}${reportRows("income_detail") ? " · " + reportRows("income_detail") : ""}`);
    setText("mabangSummaryStatus", `${reportStatus("income_summary")}${reportRows("income_summary") ? " · " + reportRows("income_summary") : ""}`);

    const vStatus = validation.status;
    setText("mabangValidationStatus", vStatus === "passed" ? "校验通过" : vStatus === "failed" ? "校验未通过" : "待运行");
    setText(
      "mabangOrderMetric",
      vStatus ? `明细 ${validation.detail_order_count ?? "—"} / 汇总 ${validation.summary_order_count ?? "—"}（差 ${validation.order_count_diff ?? "—"}）` : "—"
    );
    setText(
      "mabangReceivableMetric",
      vStatus ? `差异 ${formatMoney(validation.receivable_diff)}` : "—"
    );

    const mabangPhase = $("mabangPhaseStatus");
    if (mabangPhase) {
      const cls = vStatus === "passed" ? "success" : vStatus === "failed" ? "failed" : "pending";
      mabangPhase.className = `collection-state ${cls}`;
      mabangPhase.innerHTML = `<i></i>${vStatus === "passed" ? "校验通过" : vStatus === "failed" ? "校验未通过" : "待运行"}`;
    }

    // 步骤四：三段退款与合并
    setText("mabangRefundPart1Status", reportStatus("refund_part_01_10"));
    setText("mabangRefundPart2Status", reportStatus("refund_part_11_20"));
    setText("mabangRefundPart3Status", reportStatus("refund_part_21_end"));
    setText("mabangRefundMergedStatus", reportStatus("refunds_merged"));
    const mergedRows = reportRows("refunds_merged");
    if (mergedRows) setText("mabangRefundMergeMetric", `合并去重后 ${mergedRows}`);

    const finalPhase = $("finalPhaseStatus");
    if (finalPhase) {
      const finalStatus = (manifest.final || {}).status;
      const cls = finalStatus === "ready" ? "success" : finalStatus === "failed" ? "failed" : finalStatus === "running" ? "running" : "pending";
      const label = finalStatus === "ready" ? "已生成" : finalStatus === "failed" ? "生成失败" : finalStatus === "running" ? "生成中" : "待运行";
      finalPhase.className = `collection-state ${cls}`;
      finalPhase.innerHTML = `<i></i>${label}`;
    }
  }

  async function loadAnomalies() {
    if (!manifestPath) return;
    const category = $("anomalyCategoryFilter").value;
    const severity = $("anomalySeverityFilter").value;
    
    const res = await HQYL.api().get_vietnam_report_anomalies({
      manifest_path: manifestPath,
      category,
      severity,
      page: anomalyPage,
      page_size: anomalyPageSize
    });
    
    if (res.ok) {
      renderAnomaliesList(res.items || []);
      const start = (anomalyPage - 1) * anomalyPageSize + 1;
      const end = Math.min(start + (res.items || []).length - 1, res.total);
      $("anomalyCountInfo").textContent = res.total > 0 ? `第 ${start} - ${end} 项，共 ${res.total} 项` : `第 0 - 0 项，共 0 项`;
      $("prevAnomalyPageBtn").disabled = (anomalyPage <= 1);
      $("nextAnomalyPageBtn").disabled = (anomalyPage * anomalyPageSize >= res.total);
    }
  }

  async function loadHistoryRuns() {
    if (!outputDir) return;
    const res = await HQYL.api().list_vietnam_report_runs({
      output_dir: outputDir
    });
    if (res.ok) {
      const runs = res.runs || [];
      const listEl = $("historyRunsList");
      if (runs.length === 0) {
        listEl.innerHTML = '<div class="text-muted text-center" style="padding: 2rem 0;">无历史处理记录</div>';
        return;
      }
      
      const stateLabels = {
        created: "已创建",
        running: "运行中",
        income_ready: "待最终处理",
        mabang_ready: "可生成",
        final_ready: "已生成"
      };
      
      listEl.innerHTML = runs.map((run) => {
        const isActive = run.manifest_path === manifestPath ? "active" : "";
        const label = stateLabels[run.status] || run.status;
        return `
          <div class="history-item ${isActive}" data-path="${escapeHtml(run.manifest_path)}">
            <div>
              <strong>${escapeHtml(run.updated_at || run.period)}</strong>
              <small style="display:block;color:#64748b;">${escapeHtml(label)} · ${escapeHtml(run.store_count ?? 0)} 家店铺</small>
            </div>
          </div>`;
      }).join("");
      
      listEl.querySelectorAll(".history-item").forEach(item => {
        item.addEventListener("click", async () => {
          const path = item.getAttribute("data-path");
          userPinnedTab = false;
          userPinnedStep = false;
          lastDerivedKind = "";
          activeTask = null;
          HQYL.state.taskId = "";
          if ($("logBox")) $("logBox").textContent = "";
          await loadDashboard(path);
          applyDerivedState(currentDashboard, "batch");
        });
      });
    }
  }

  async function saveProfitRates() {
    if (!manifestPath) return;
    const lines = $("profitRateInput").value.split(/\r?\n/);
    const rates = {};
    for (const line of lines) {
      const match = line.split("=");
      if (match.length === 2) {
        const key = match[0].trim();
        const value = match[1].trim();
        if (key && value) {
          rates[key] = value;
        }
      }
    }
    const res = await HQYL.api().save_vietnam_report_config({
      manifest_path: manifestPath,
      profit_rates: rates
    });
    if (!res.ok) {
      HQYL.showToast(res.error || "保存利润率失败");
    } else {
      HQYL.showToast("利润率已自动保存并重新计算");
    }
  }

  async function chooseOutputDir() {
    const result = await HQYL.api().choose_output_dir($("outputDir").value || outputDir);
    if (result.ok) {
      outputDir = result.path;
      $("outputDir").value = result.path;
      await loadHistoryRuns();
    } else if (!result.cancelled) {
      HQYL.showToast(result.error || "选择目录失败");
    }
  }

  function loadDemoStores() {
    $("storeInput").value = "越南普通店-演示\n越南仓发店-演示\n越南普通店二号-演示";
    manifestPath = "";
    runDir = "";
    $("openRunDirBtn").disabled = true;
    $("openManifestBtn").disabled = true;
    $("startSubsidyBtn").disabled = true;
    $("startSourcesBtn").disabled = true;
    renderDraftMatrix(draftManifest(parseStores()));
    HQYL.appendLog("已填入 3 个演示店铺；创建批次不会实际启动浏览器");
  }

  function setCreating(creating) {
    $("createRunBtn").disabled = creating;
    $("chooseDirBtn").disabled = creating;
    $("demoStoresBtn").disabled = creating;
    $("collectionPeriod").disabled = creating;
    $("outputDir").disabled = creating;
    $("storeInput").disabled = creating;
    $("createRunBtn").textContent = creating ? "正在创建..." : "生成/选定采集批次";
  }

  function setCollecting(collecting) {
    taskRunning = collecting;
    $("startSubsidyBtn").disabled = collecting || !manifestPath;
    $("startSourcesBtn").disabled = collecting || !manifestPath;
    $("createRunBtn").disabled = collecting;
    $("chooseDirBtn").disabled = collecting;
    $("demoStoresBtn").disabled = collecting;
    $("startSubsidyBtn").textContent = collecting ? "正在采集..." : "采集补贴订单";
    $("startSourcesBtn").textContent = collecting ? "正在采集..." : "采集广告/联盟/补贴";
    $("startMabangIncomeBtn").disabled = collecting || !manifestPath;
    $("startMabangIncomeBtn").textContent = collecting ? "正在运行..." : "开始导出收支并校验";
    $("startMabangRefundBtn").disabled = collecting || !manifestPath;
    $("startMabangRefundBtn").textContent = collecting ? "正在运行..." : "合并马帮退款";
    $("startFinalProcessBtn").disabled = collecting || !manifestPath;
    $("startFinalProcessBtn").textContent = collecting ? "正在运行..." : "生成核对工作簿 (Excel)";
  }

  async function createRun() {
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", createRun);
      return;
    }

    try {
      const stores = parseStores();
      if (!stores.length) throw new Error("请至少添加一个越南 Shopee 店铺名称");
      if (!$("collectionPeriod").value) throw new Error("请选择核对月份");
      if (!$("outputDir").value.trim()) throw new Error("请选择输出位置");

      setCreating(true);
      setPageBadge("running", "正在创建");
      HQYL.appendLog(`正在创建 ${$("collectionPeriod").value} 采集批次`);
      const result = await HQYL.api().create_vietnam_collection_run({
        account_id: account.id,
        period: $("collectionPeriod").value,
        output_dir: $("outputDir").value.trim(),
        stores,
      });
      if (!result.ok) throw new Error(result.error || "创建批次失败");

      runDir = result.run_dir || "";
      manifestPath = result.manifest_path || "";
      
      await loadDashboard(manifestPath);
      setPageBadge("success", "批次已创建");
      HQYL.appendLog(`采集批次创建成功，准备进入数据采集模块。`);
      HQYL.showToast("采集批次已初始化");
    } catch (error) {
      setPageBadge("failed", "创建失败");
      HQYL.appendLog(error.message || String(error));
      HQYL.showToast(error.message || String(error));
    } finally {
      setCreating(false);
    }
  }

  async function openManifest() {
    if (!manifestPath) return;
    const result = await HQYL.api().open_path(manifestPath);
    if (!result.ok) HQYL.showToast(result.error || "打开清单文件失败");
  }

  async function startSubsidyCollection() {
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", startSubsidyCollection);
      return;
    }
    if (!manifestPath) {
      HQYL.showToast("请先生成/选择采集批次");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备使用 Playwright 采集补贴订单...");
      return HQYL.api().start_vietnam_subsidy_collection({
        account_id: account.id,
        manifest_path: manifestPath,
      });
    });
  }

  async function startSourcesCollection() {
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", startSourcesCollection);
      return;
    }
    if (!manifestPath) {
      HQYL.showToast("请先生成/选择采集批次");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备采集广告、联盟及广告补贴数据源...");
      return HQYL.api().start_vietnam_ziniao_sources_collection({
        account_id: account.id,
        manifest_path: manifestPath,
      });
    });
  }

  async function startMabangIncomeCollection() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", startMabangIncomeCollection);
      return;
    }
    if (!manifestPath) {
      HQYL.showToast("请先创建/指定采集批次");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("拉取马帮 Shopee 越南收支明细快照并执行校验...");
      return HQYL.api().start_vietnam_mabang_income_collection({
        account_id: account.id,
        manifest_path: manifestPath,
      });
    });
  }

  async function startMabangRefundCollection() {
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", startMabangRefundCollection);
      return;
    }
    if (!manifestPath) {
      HQYL.showToast("请先完成前置收支校验");
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("拉取马帮三段已发货退款订单数据并去重合并...");
      return HQYL.api().start_vietnam_mabang_refund_collection({
        account_id: account.id,
        manifest_path: manifestPath,
      });
    });
  }

  async function startFinalReconciliation() {
    if (!manifestPath) {
      HQYL.showToast("请先完成前置数据采集");
      return;
    }
    // Automatically save profit rates configuration first
    await saveProfitRates();

    // 同时把利润率序列化后随请求下发，避免后端只拿到 manifest_path 而读不到刚填写的利润率
    const profitRatesText = $("profitRateInput").value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.includes("=") && line.split("=")[1].trim())
      .join("\n");

    await HQYL.startTask(() => {
      HQYL.appendLog("准备生成越南 Shopee 收支报表工作簿...");
      return HQYL.api().start_vietnam_final_reconciliation({
        manifest_path: manifestPath,
        profit_rates_text: profitRatesText,
      });
    });
  }

  const page = {
    key: "vietnam_income_reconciliation",
    taskKey: "vietnam_subsidy_collection",
    taskKeys: [
      "vietnam_subsidy_collection",
      "vietnam_ziniao_sources_collection",
      "vietnam_mabang_income_collection",
      "vietnam_mabang_refund_collection",
      "vietnam_final_reconciliation",
    ],
    taskContextFilter: (context) => !manifestPath || !context?.manifest_path || context.manifest_path === manifestPath,
    title: "越南收支报表",
    async init(info) {
      const collectionInfo = await HQYL.api().get_vietnam_collection_info();
      const settings = info.settings || {};
      
      outputDir = collectionInfo.output_dir || settings.output_dir || "";
      $("outputDir").value = outputDir;
      $("collectionPeriod").value = collectionInfo.period || "";
      $("periodText").textContent = collectionInfo.period || "—";
      
      // Bind basic click events
      $("collectionBatchForm").addEventListener("submit", (event) => { event.preventDefault(); createRun(); });
      $("chooseDirBtn").addEventListener("click", chooseOutputDir);
      $("demoStoresBtn").addEventListener("click", loadDemoStores);
      $("openRunDirBtn").addEventListener("click", () => HQYL.openOutput(runDir));
      $("openManifestBtn").addEventListener("click", openManifest);
      $("startSubsidyBtn").addEventListener("click", startSubsidyCollection);
      $("startSourcesBtn").addEventListener("click", startSourcesCollection);
      $("startMabangIncomeBtn").addEventListener("click", startMabangIncomeCollection);
      $("startMabangRefundBtn").addEventListener("click", startMabangRefundCollection);
      $("startFinalProcessBtn").addEventListener("click", startFinalReconciliation);
      $("retryValidationBtn").addEventListener("click", startMabangIncomeCollection);
      $("openOutputFileBtn").addEventListener("click", () => HQYL.openOutput(currentDashboard?.files?.output_file));
      $("openUnmatchedFileBtn").addEventListener("click", () => HQYL.openOutput(currentDashboard?.files?.unmatched_file));
      $("openProcessingLogBtn").addEventListener("click", () => HQYL.openOutput(currentDashboard?.files?.processing_log));
      $("openResultDirBtn").addEventListener("click", () => HQYL.openOutput(currentDashboard?.files?.run_dir));
      $("storeSearchInput").addEventListener("input", renderStoreTable);
      $("storeStatusFilter").addEventListener("change", renderStoreTable);
      $("clearStoreFiltersBtn").addEventListener("click", () => {
        $("storeSearchInput").value = "";
        $("storeStatusFilter").value = "all";
        renderStoreTable();
      });
      document.querySelectorAll(".table-sort").forEach((button) => button.addEventListener("click", () => {
        const key = button.dataset.sort;
        storeSort = { key, direction: storeSort.key === key && storeSort.direction === "desc" ? "asc" : "desc" };
        renderStoreTable();
      }));
      [["tabOverview", "overview"], ["tabAnomalies", "anomalies"], ["tabDataPrep", "dataPrep"]].forEach(([id, name]) => {
        $(id).addEventListener("click", () => { userPinnedTab = true; activateTab(name); });
      });
      document.querySelectorAll(".wizard-step").forEach((item) => item.addEventListener("click", () => {
        userPinnedStep = true;
        activateStep(Number(item.dataset.step));
      }));
      
      // Auto-save profit rates on blur
      $("profitRateInput").addEventListener("blur", saveProfitRates);
      
      // Filter anomalies listeners
      $("anomalySeverityFilter").addEventListener("change", () => { anomalyPage = 1; loadAnomalies(); });
      $("anomalyCategoryFilter").addEventListener("change", () => { anomalyPage = 1; loadAnomalies(); });
      $("prevAnomalyPageBtn").addEventListener("click", () => { if (anomalyPage > 1) { anomalyPage--; loadAnomalies(); } });
      $("nextAnomalyPageBtn").addEventListener("click", () => { anomalyPage++; loadAnomalies(); });

      // Handle store typing preview draft matrices
      $("storeInput").addEventListener("input", () => {
        manifestPath = "";
        runDir = "";
        $("openRunDirBtn").disabled = true;
        $("openManifestBtn").disabled = true;
        $("startSubsidyBtn").disabled = true;
        $("startSourcesBtn").disabled = true;
        $("startMabangIncomeBtn").disabled = true;
        $("startMabangRefundBtn").disabled = true;
        $("startFinalProcessBtn").disabled = true;
        setPageBadge("idle", "待创建");
        renderDraftMatrix(draftManifest(parseStores()));
      });

      $("collectionPeriod").addEventListener("change", () => {
        $("periodText").textContent = $("collectionPeriod").value || "—";
        if (!manifestPath) renderDraftMatrix(draftManifest(parseStores()));
      });

      // Restore last manifest path if stored in AppSettings
      const lastManifest = settings.vietnam_last_manifest_path || "";
      if (lastManifest) {
        await loadDashboard(lastManifest);
      } else {
        renderDraftMatrix(draftManifest([]));
        setPageBadge("idle", "待创建");
        currentDashboard = { state: "empty", data_progress: { completed: 0, total: 0 }, files: {}, config: { profit_rates: {} } };
        applyDerivedState(currentDashboard, "initial");
      }

      await loadHistoryRuns();
      HQYL.appendLog("越南 Shopee 收支报表模块加载完毕，等待初始化。");
    },
    setRunning(running) {
      setCollecting(running);
    },
    onTaskRestored(task) {
      activeTask = task;
      if (currentDashboard) applyDerivedState(currentDashboard, "initial");
    },
    resetResult() {},
    async applyResult(result) {
      activeTask = null;
      if (result.manifest_path) manifestPath = result.manifest_path;
      runDir = result.run_dir || runDir;
      if (result.output_file) {
        HQYL.appendLog(`最终报表文件已生成：${result.output_file}`);
        HQYL.showToast("收支报表工作簿已成功生成！");
      }
      await loadDashboard(manifestPath);
    },
    onAccountsChanged() {
      if (manifestPath) {
        loadDashboard(manifestPath);
      }
    },
  };

  HQYL.boot(page);
})();
