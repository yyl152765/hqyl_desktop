(() => {
  const $ = HQYL.$;
  const ORDER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_\-]{3,63}$/;
  const MAX_BATCH = 2000;

  let outputDir = "";
  let resultOutputDir = "";
  let outputFile = "";
  let fieldDefinitions = [];
  let defaultFields = [];
  let parsedFile = null;
  let running = false;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  function basename(path) {
    return String(path || "").split(/[\\/]/).pop() || "";
  }

  // ---------------------------------------------------------------------
  // 输入区
  // ---------------------------------------------------------------------

  function pastedOrderNumbers() {
    const seen = new Set();
    const numbers = [];
    $("orderText").value
      .split(/[\s,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean)
      .forEach((item) => {
        if (!ORDER_PATTERN.test(item) || seen.has(item)) return;
        seen.add(item);
        numbers.push(item);
      });
    return numbers;
  }

  function currentOrderCount() {
    if (parsedFile && parsedFile.order_count) return Number(parsedFile.order_count) || 0;
    return pastedOrderNumbers().length;
  }

  function updateOrderCount() {
    const count = currentOrderCount();
    $("orderCount").textContent = String(count);
    if (parsedFile) {
      const parts = [`有效 ${count} 个`];
      if (parsedFile.duplicate_count) parts.push(`重复 ${parsedFile.duplicate_count} 行`);
      if (parsedFile.invalid_count) parts.push(`无效 ${parsedFile.invalid_count} 行`);
      $("orderCountHint").textContent = parts.join(" · ");
    } else if (count) {
      $("orderCountHint").textContent = count > MAX_BATCH ? `将自动分 ${Math.ceil(count / MAX_BATCH)} 批查询` : "已粘贴订单编号";
    } else {
      $("orderCountHint").textContent = "等待上传或粘贴";
    }
    $("fileParseSummary").textContent = parsedFile
      ? `已读取 ${parsedFile.name}${parsedFile.sheet_name ? `（Sheet「${parsedFile.sheet_name}」` : "（"}${parsedFile.column_name ? `，列「${parsedFile.column_name}」）` : "）"}：有效 ${count} 个订单编号`
      : "尚未读取订单编号文件；也可以直接在下方粘贴订单编号";
  }

  async function chooseOrderFile() {
    try {
      const result = await HQYL.api().choose_mabang_order_file();
      if (result.cancelled) return;
      if (!result.ok) {
        HQYL.showToast(result.error || "读取订单编号文件失败");
        return;
      }
      parsedFile = result;
      $("orderFilePath").value = result.path || "";
      $("orderText").value = "";
      updateOrderCount();
      if (!Number(result.order_count)) HQYL.showToast("文件中没有解析到有效订单编号");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function chooseOutputDir() {
    try {
      const result = await HQYL.api().choose_scoped_output_dir({
        tool: "mabang_order_sku_export",
        kind: "output",
        current_dir: $("orderOutputDir").value.trim() || outputDir,
      });
      if (result.ok) {
        outputDir = result.output_dir || result.path || outputDir;
        $("orderOutputDir").value = outputDir;
      } else if (!result.cancelled) {
        HQYL.showToast(result.error || "选择目录失败");
      }
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  // ---------------------------------------------------------------------
  // 导出字段
  // ---------------------------------------------------------------------

  function renderFields() {
    const container = $("fieldOptions");
    const selected = new Set(defaultFields);
    container.innerHTML = fieldDefinitions
      .map((field) => {
        const locked = field.key === "sku";
        const checked = locked || selected.has(field.key);
        return `<label class="field-option${checked ? " is-checked" : ""}${locked ? " is-locked" : ""}" data-key="${escapeHtml(field.key)}"${locked ? ' title="SKU 为固定导出字段，汇总时按它去重"' : ""}>
          <input type="checkbox" value="${escapeHtml(field.key)}" ${checked ? "checked" : ""} ${locked ? "disabled" : ""} />
          <span>${escapeHtml(field.label)}${locked ? "（固定）" : ""}</span>
        </label>`;
      })
      .join("");
    container.querySelectorAll(".field-option").forEach((label) => {
      label.addEventListener("click", (event) => {
        if (label.classList.contains("is-locked")) {
          event.preventDefault();
          return;
        }
      });
      const input = label.querySelector("input");
      input.addEventListener("change", () => {
        label.classList.toggle("is-checked", input.checked);
      });
    });
  }

  function selectedFields() {
    const keys = Array.from($("fieldOptions").querySelectorAll("input:checked")).map((input) => input.value);
    if (!keys.includes("sku")) keys.unshift("sku");
    return keys;
  }

  function setAllFields(checked) {
    $("fieldOptions").querySelectorAll(".field-option").forEach((label) => {
      if (label.classList.contains("is-locked")) return;
      const input = label.querySelector("input");
      input.checked = checked;
      label.classList.toggle("is-checked", checked);
    });
  }

  // ---------------------------------------------------------------------
  // 运行与结果
  // ---------------------------------------------------------------------

  async function runExport() {
    if (running) return;
    const count = currentOrderCount();
    if (!count) {
      HQYL.showToast("请先选择订单编号文件，或粘贴订单编号");
      $("orderText").focus();
      return;
    }
    const selectedOutputDir = $("orderOutputDir").value.trim() || outputDir;
    if (!selectedOutputDir) {
      HQYL.showToast("请选择输出目录");
      return;
    }
    outputDir = selectedOutputDir;

    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runExport);
      return;
    }

    const payload = {
      account_id: account.id,
      output_dir: selectedOutputDir,
      fields: selectedFields(),
      include_detail: $("includeDetail").checked,
      include_not_found: $("includeNotFound").checked,
    };
    if (parsedFile && parsedFile.path) payload.source_file = parsedFile.path;
    else payload.order_text = $("orderText").value;

    await HQYL.startTask(() => {
      HQYL.appendLog(`准备处理 ${count} 个订单编号，导出字段：${selectedFields().length} 个`);
      HQYL.appendLog("流程：按订单编号批量查询 → 抓取订单商品明细 → 汇总唯一 SKU");
      return HQYL.api().start_mabang_order_sku_export(payload);
    });
  }

  function renderResultTable(rows) {
    const body = $("resultTableBody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="5" class="result-empty">暂无数据</td></tr>';
      return;
    }
    body.innerHTML = rows
      .map(
        (row) => `<tr>
          <td class="sku-cell">${escapeHtml(row.sku)}</td>
          <td>${escapeHtml(row.product_name)}</td>
          <td class="num-cell">${escapeHtml(row.order_count)}</td>
          <td class="num-cell">${escapeHtml(row.total_quantity)}</td>
          <td class="order-cell">${escapeHtml(row.order_numbers || "")}</td>
        </tr>`,
      )
      .join("");
  }

  function clearResultTable() {
    renderResultTable([]);
    $("resultTableHint").textContent = "查询完成后展示去重后的 SKU 清单（最多 200 行）";
  }

  const page = {
    key: "mabang_order_sku_export",
    taskKeys: ["mabang_order_sku_export"],
    title: "马帮订单SKU汇总",
    taskContextFilter(context) {
      const account = HQYL.activeAccount("mabang");
      return !context.account_id || String(context.account_id) === String(account?.id || "");
    },
    async init() {
      $("orderSkuForm").addEventListener("submit", (event) => {
        event.preventDefault();
        runExport();
      });
      $("chooseOrderFileBtn").addEventListener("click", chooseOrderFile);
      $("chooseOutputDirBtn").addEventListener("click", chooseOutputDir);
      $("selectAllFieldsBtn").addEventListener("click", () => setAllFields(true));
      $("resetFieldsBtn").addEventListener("click", () => {
        $("fieldOptions").querySelectorAll(".field-option").forEach((label) => {
          const key = label.dataset.key;
          const input = label.querySelector("input");
          if (label.classList.contains("is-locked")) return;
          input.checked = defaultFields.includes(key);
          label.classList.toggle("is-checked", input.checked);
        });
      });
      $("orderText").addEventListener("input", () => {
        if (parsedFile) {
          parsedFile = null;
          $("orderFilePath").value = "";
        }
        updateOrderCount();
      });
      $("clearOrderInputBtn").addEventListener("click", () => {
        parsedFile = null;
        $("orderText").value = "";
        $("orderFilePath").value = "";
        updateOrderCount();
      });
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(resultOutputDir || outputFile || outputDir));

      try {
        const info = await HQYL.api().get_mabang_order_sku_export_info();
        if (info.ok) {
          fieldDefinitions = Array.isArray(info.fields) ? info.fields : [];
          defaultFields = Array.isArray(info.default_fields) ? info.default_fields : [];
          renderFields();
          outputDir = info.output_dir || outputDir;
          if (!$("orderOutputDir").value) $("orderOutputDir").value = outputDir;
        } else {
          $("fieldOptions").innerHTML = `<span class="field-picker-hint">${escapeHtml(info.error || "读取导出字段失败")}</span>`;
        }
      } catch (error) {
        $("fieldOptions").innerHTML = `<span class="field-picker-hint">${escapeHtml(error.message || String(error))}</span>`;
      }
      updateOrderCount();
    },
    setRunning(value) {
      running = Boolean(value);
      $("runOrderSkuBtn").disabled = running;
      $("clearOrderInputBtn").disabled = running;
      $("chooseOrderFileBtn").disabled = running;
      $("chooseOutputDirBtn").disabled = running;
      $("orderText").disabled = running;
      $("includeDetail").disabled = running;
      $("includeNotFound").disabled = running;
    },
    resetResult() {
      outputFile = "";
      resultOutputDir = "";
      $("uniqueSkuCount").textContent = "0";
      $("detailHint").textContent = "订单明细 0 行";
      $("resultSummary").textContent = "尚未生成文件";
      $("openOutputBtn").disabled = true;
      clearResultTable();
      updateOrderCount();
    },
    onTaskRestored(task) {
      const context = task?.context || {};
      const count = Number(context.order_count || 0);
      if (count > 0) {
        $("orderCount").textContent = String(count);
        $("orderCountHint").textContent = "上次任务的订单编号数量";
      }
      if (context.source_file) $("orderFilePath").value = context.source_file;
      if (context.output_dir) {
        outputDir = context.output_dir;
        $("orderOutputDir").value = outputDir;
      }
    },
    applyResult(result) {
      outputFile = result.output_file || "";
      resultOutputDir = result.output_dir || "";
      if (resultOutputDir) {
        outputDir = resultOutputDir;
        $("orderOutputDir").value = resultOutputDir;
      }
      $("uniqueSkuCount").textContent = String(result.unique_sku_count ?? 0);
      $("detailHint").textContent = `订单明细 ${result.detail_row_count ?? 0} 行 · 命中订单 ${result.matched_order_count ?? 0} 单`;
      $("outputFile").textContent = outputFile ? basename(outputFile) : "未生成";
      $("orderCount").textContent = String(result.input_order_count ?? currentOrderCount());

      const notFoundCount = Number(result.not_found_count || 0);
      const summaryParts = [
        `输入 ${result.input_order_count ?? 0} 个订单号`,
        `命中 ${result.matched_order_count ?? 0} 单`,
        `明细 ${result.detail_row_count ?? 0} 行`,
        `唯一 SKU ${result.unique_sku_count ?? 0} 个`,
      ];
      if (notFoundCount) summaryParts.push(`未查询到 ${notFoundCount} 个`);
      $("resultSummary").textContent = summaryParts.join(" · ") + (outputFile ? `；文件：${basename(outputFile)}` : "");

      const uniqueSkus = Array.isArray(result.unique_skus) ? result.unique_skus : [];
      renderResultTable(uniqueSkus);
      $("resultTableHint").textContent = result.unique_sku_truncated
        ? `共 ${result.unique_sku_count} 个唯一 SKU，页面展示前 ${uniqueSkus.length} 个（完整清单见 Excel）`
        : `共 ${result.unique_sku_count} 个唯一 SKU，重复项已合并为一条`;
      $("openOutputBtn").disabled = !(resultOutputDir || outputFile);
    },
  };

  HQYL.boot(page);
})();
