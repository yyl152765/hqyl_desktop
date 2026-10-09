(() => {
  const $ = HQYL.$;
  let outputRoot = "";
  let resultOutputDir = "";
  let outputFile = "";
  let basisText = "实际体积（长 × 宽 × 高 × 数量 × 0.000001）";

  const SUMMARY_ROWS = [
    { label: "仓储费", billed: "仓储费_账单金额", expected: "仓储费_核对金额", count: "仓储费_异常数" },
    { label: "卸货费", billed: "卸货费_账单金额", expected: "卸货费_核对金额", count: "卸货费_异常数" },
    { label: "出库+耗材", billed: "出库+耗材_账单金额", expected: "出库+耗材_核对金额", count: "出库+耗材_异常数" },
    { label: "退件上架费", billed: "退件上架费_账单金额", expected: "退件上架费_核对金额", count: "退件上架费_待确认数" },
    { label: "杂费", billed: "杂费_账单金额", expected: "杂费_核对金额", count: "杂费_待确认数" },
  ];

  const AUX_ROWS = [
    ["核算基数", "仓储费核算基数"],
    ["仓储费体积差额合计(CBM)", "仓储费_体积差额合计"],
    ["仓储费缺失尺寸行数", "仓储费_缺失尺寸行数"],
    ["出库订单数", "出库订单数"],
    ["出库月均日单量", "出库月均日单量"],
    ["出库未命中订单数", "出库+耗材_未命中订单数"],
    ["出库数量不一致数", "出库+耗材_数量不一致数"],
    ["退件待确认数", "退件上架费_待确认数"],
  ];

  function basename(path) {
    return String(path || "").split(/[\\/]/).pop() || "";
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function money(value) {
    return number(value).toFixed(2);
  }

  function decimal(value, digits = 6) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    return parsed.toFixed(digits);
  }

  function setBillNotice(text, warning = false) {
    const box = $(warning ? "kecBillWarning" : "kecBillInfo");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text || "";
  }

  function applyBillPreview(response) {
    const sheets = response.sheet_titles || {};
    const parts = Object.keys(sheets).map((key) => `${key}：${sheets[key]}`);
    const warnings = Array.isArray(response.warnings) ? response.warnings.filter(Boolean).map(String) : [];
    setBillNotice(parts.length ? `已识别工作表：${parts.join("；")}` : "");
    setBillNotice(warnings.join("；"), true);
    $("kecBillHint").textContent = warnings.length
      ? "账单已读取，但存在需要注意的问题，请查看下方提示。"
      : `已读取账单：${response.name || basename(response.input_file)}`;
  }

  async function chooseBill() {
    const result = await HQYL.api().choose_kec_reconciliation_file({ kind: "input" });
    if (result.cancelled) return;
    if (!result.ok) {
      HQYL.showToast(result.error || "选择账单失败");
      return;
    }
    $("kecBillFile").value = result.path;
    applyBillPreview(result);
  }

  async function chooseQuote() {
    const result = await HQYL.api().choose_kec_reconciliation_file({ kind: "quote" });
    if (result.cancelled) return;
    if (!result.ok) {
      HQYL.showToast(result.error || "选择报价表失败");
      return;
    }
    $("kecQuoteFile").value = result.path;
    $("kecQuoteHint").textContent = `已选择报价表：${result.name || basename(result.path)}`;
  }

  async function chooseOutputDir() {
    const result = await HQYL.api().choose_output_dir($("kecOutputDir").value.trim() || outputRoot);
    if (result.ok) {
      outputRoot = result.path;
      $("kecOutputDir").value = result.path;
    } else if (!result.cancelled) {
      HQYL.showToast(result.error || "选择目录失败");
    }
  }

  async function inspectBill() {
    const input = $("kecBillFile").value.trim();
    if (!input) {
      setBillNotice("");
      setBillNotice("", true);
      return;
    }
    try {
      const response = await HQYL.api().inspect_kec_reconciliation_source({ input_file: input });
      if (!response.ok) {
        setBillNotice("");
        setBillNotice(response.error || "读取账单失败", true);
        return;
      }
      applyBillPreview({ ...response, name: basename(response.input_file) });
    } catch (error) {
      setBillNotice(error.message || String(error), true);
    }
  }

  async function runReconciliation() {
    const inputFile = $("kecBillFile").value.trim();
    if (!inputFile) {
      HQYL.showToast("请选择或粘贴 KEC 账单 Excel 路径");
      return;
    }
    const quoteFile = $("kecQuoteFile").value.trim();
    const selectedOutputRoot = $("kecOutputDir").value.trim() || outputRoot;
    if (!selectedOutputRoot) {
      HQYL.showToast("请选择输出目录");
      return;
    }
    outputRoot = selectedOutputRoot;
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runReconciliation);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(`仓租费核算基数：${basisText}`);
      HQYL.appendLog(quoteFile ? `使用报价表：${quoteFile}` : "未提供报价表，使用程序内置报价规则");
      return HQYL.api().start_kec_reconciliation({
        account_id: account.id,
        input_file: inputFile,
        quote_file: quoteFile,
        output_dir: selectedOutputRoot,
      });
    });
  }

  function renderSummary(summary) {
    const body = $("kecSummaryTableBody");
    body.innerHTML = "";
    const stats = summary || {};
    SUMMARY_ROWS.forEach((row) => {
      const billed = stats[row.billed];
      const expected = stats[row.expected];
      const diff = number(billed) - number(expected);
      const tr = document.createElement("tr");
      const cells = [
        row.label,
        money(billed),
        money(expected),
        money(diff),
        String(stats[row.count] ?? 0),
      ];
      cells.forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 3 && diff !== 0) td.style.fontWeight = "600";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    $("kecResultEmpty").hidden = SUMMARY_ROWS.length > 0;
  }

  function renderAux(stats) {
    const lines = AUX_ROWS
      .filter(([, key]) => stats[key] !== undefined && stats[key] !== null && stats[key] !== "")
      .map(([label, key]) => `${label}：${typeof stats[key] === "number" ? decimal(stats[key]) : stats[key]}`);
    const box = $("kecAuxSummary");
    box.hidden = !lines.length;
    box.textContent = lines.join("；");
  }

  function renderOverview(result) {
    $("storageBilledAmount").textContent = money(result.storage_billed_amount);
    $("storageExpectedAmount").textContent = money(result.storage_expected_amount);
    const stats = result.summary || {};
    const volumeDiff = stats["仓储费_体积差额合计"];
    $("storageVolumeDiff").textContent = volumeDiff === undefined || volumeDiff === null ? "—" : decimal(volumeDiff);
  }

  const page = {
    key: "kec_reconciliation",
    taskKeys: ["kec_reconciliation"],
    title: "KEC 对账",
    taskContextFilter(context) {
      const account = HQYL.activeAccount("mabang");
      return !context.account_id || String(context.account_id) === String(account?.id || "");
    },
    async init(info) {
      outputRoot = info.settings?.output_dir || "";
      $("kecOutputDir").value = outputRoot;
      try {
        const response = await HQYL.api().get_kec_reconciliation_info();
        if (response?.ok && response.basis) {
          basisText = response.basis;
          $("kecBasisRule").textContent = `仓租费核算基数：${response.basis}`;
        }
      } catch (_error) {
        // 使用内置说明文案即可，不阻塞页面。
      }
      $("kecReconciliationForm").addEventListener("submit", (event) => {
        event.preventDefault();
        runReconciliation();
      });
      $("kecChooseBillBtn").addEventListener("click", chooseBill);
      $("kecChooseQuoteBtn").addEventListener("click", chooseQuote);
      $("kecChooseDirBtn").addEventListener("click", chooseOutputDir);
      $("kecBillFile").addEventListener("change", inspectBill);
      $("kecOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(resultOutputDir || outputFile || outputRoot));
      $("kecOpenFileBtn").addEventListener("click", () => HQYL.openOutput(outputFile));
    },
    setRunning(running) {
      $("kecRunBtn").disabled = running;
      $("kecBillFile").disabled = running;
      $("kecQuoteFile").disabled = running;
      $("kecOutputDir").disabled = running;
      $("kecChooseBillBtn").disabled = running;
      $("kecChooseQuoteBtn").disabled = running;
      $("kecChooseDirBtn").disabled = running;
    },
    resetResult() {
      resultOutputDir = "";
      outputFile = "";
      $("storageBilledAmount").textContent = "0.00";
      $("storageExpectedAmount").textContent = "0.00";
      $("storageVolumeDiff").textContent = "0.00";
      $("outputFile").textContent = "未生成";
      $("kecResult").hidden = true;
      $("kecResultSummary").textContent = "暂无结果";
      $("kecSummaryTableBody").innerHTML = "";
      $("kecAuxSummary").hidden = true;
      $("kecOpenFileBtn").disabled = true;
      $("kecOpenOutputBtn").disabled = true;
    },
    onTaskRestored(task) {
      const restoredRoot = String(task?.context?.output_dir || "");
      if (restoredRoot && !$("kecOutputDir").value.trim()) {
        outputRoot = restoredRoot;
        $("kecOutputDir").value = restoredRoot;
      }
      const restoredInput = String(task?.context?.input_file || "");
      if (restoredInput && !$("kecBillFile").value.trim()) $("kecBillFile").value = restoredInput;
    },
    applyResult(result) {
      if (!result || result.success === false) return;
      outputFile = String(result.output_file || "");
      resultOutputDir = outputFile ? String(outputFile).replace(/[\\/][^\\/]*$/, "") : "";
      $("outputFile").textContent = outputFile ? basename(outputFile) : "未生成";
      renderOverview(result);
      renderSummary(result.summary);
      renderAux(result.summary || {});
      $("kecResult").hidden = false;
      $("kecResultSummary").textContent = `月均日单量 ${decimal(result.avg_daily_orders, 2)}，出库操作费 ${money(result.outbound_base_fee)} 元/票`;
      $("kecOpenFileBtn").disabled = !outputFile;
      $("kecOpenOutputBtn").disabled = !resultOutputDir;
    },
  };

  HQYL.boot(page);
})();
