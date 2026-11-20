(() => {
  const $ = HQYL.$;
  const MODE_DECLARE = "declare";
  const MODE_EXPORT = "export_only";
  const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
  const FILE_FIELDS = { workbook: "taxWorkbook", app: "taxAppPath" };
  let ready = false;
  let running = false;
  let choosing = false;
  let outputFile = "";
  let exportDir = "";

  function showError(message = "") {
    $("taxFormError").textContent = message;
    $("taxFormError").hidden = !message;
  }

  function updateControls() {
    const declaring = $("taxMode").value === MODE_DECLARE;
    const blocked = running || choosing;
    $("taxConfirmField").hidden = !declaring;
    $("taxDeclarationForm").querySelectorAll("input, select, [data-tax-file-kind]").forEach((control) => {
      control.disabled = blocked;
    });
    $("taxConfirmDeclaration").disabled = blocked || !declaring;
    $("taxRunBtn").disabled = blocked || !ready || (declaring && !$("taxConfirmDeclaration").checked);
    $("taxRunBtn").textContent = running ? "运行中" : (declaring ? "提交正式申报" : "开始导出");
    $("taxDeclarationForm").setAttribute("aria-busy", String(blocked));
    $("taxOpenResultBtn").disabled = blocked || !outputFile;
    $("taxOpenExportsBtn").disabled = blocked || !exportDir;
  }

  async function chooseFile(kind) {
    if (running || choosing) return;
    choosing = true;
    showError();
    updateControls();
    try {
      const response = await HQYL.api().choose_tax_declaration_file({ kind });
      if (response?.cancelled) return;
      if (!response?.ok || !response.path) throw new Error(response?.error || "文件未选中，请重试或输入完整路径。");
      $(FILE_FIELDS[kind]).value = response.path;
      $("taxConfirmDeclaration").checked = false;
    } catch (error) {
      showError(error.message || String(error));
    } finally {
      choosing = false;
      updateControls();
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!ready || running || choosing) return;
    showError();
    const declaring = $("taxMode").value === MODE_DECLARE;
    const confirmed = declaring && $("taxConfirmDeclaration").checked;
    const workbook = $("taxWorkbook").value.trim();
    const appPath = $("taxAppPath").value.trim();
    const start = $("taxExportStart").value;
    const end = $("taxExportEnd").value;
    const maxRows = $("taxMaxRows").value;
    if (!workbook || !appPath) return showError("请选择账号表和税务客户端。");
    if (!MONTH.test(start) || !MONTH.test(end) || start > end) return showError("请选择有效的导出月份，开始月份不能晚于结束月份。");
    if (maxRows && (!Number.isSafeInteger(Number(maxRows)) || Number(maxRows) < 1)) return showError("公司数上限必须是大于零的整数。");
    if (declaring && !confirmed) return showError("请先核对税款所属期和公司范围，再勾选正式申报确认。");

    const payload = {
      workbook,
      app_path: appPath,
      mode: declaring ? MODE_DECLARE : MODE_EXPORT,
      confirm_declaration: confirmed,
      export_start_month: start,
      export_end_month: end,
    };
    if (maxRows) payload.max_rows = Number(maxRows);
    $("taxConfirmDeclaration").checked = false;
    await HQYL.startTask(async () => {
      const task = await HQYL.api().start_tax_declaration(payload);
      if (!task?.ok) throw new Error(task?.error || task?.message || "任务未启动，请检查配置后重试。");
      const id = task.id || task.task_id;
      if (!id) throw new Error("未取得任务编号，请检查后台任务状态，不要重复提交。");
      return { ...task, id };
    });
  }

  function openFolder(path) {
    HQYL.openOutput(path).catch((error) => showError(error.message || String(error)));
  }

  const page = {
    key: "tax_declaration",
    taskKeys: ["tax_declaration"],
    title: "电子税务局申报",
    async init() {
      $("taxMode").value = MODE_EXPORT;
      $("taxConfirmDeclaration").checked = false;
      $("taxDeclarationForm").addEventListener("submit", submit);
      $("taxDeclarationForm").addEventListener("input", (event) => {
        if (event.target !== $("taxConfirmDeclaration")) $("taxConfirmDeclaration").checked = false;
        showError();
        updateControls();
      });
      $("taxOpenResultBtn").addEventListener("click", () => openFolder(outputFile));
      $("taxOpenExportsBtn").addEventListener("click", () => openFolder(exportDir));

      const bridge = HQYL.api();
      $("taxDeclarationForm").querySelectorAll("[data-tax-file-kind]").forEach((button) => {
        button.hidden = typeof bridge.choose_tax_declaration_file !== "function";
        button.addEventListener("click", () => chooseFile(button.dataset.taxFileKind));
      });
      try {
        if (typeof bridge.start_tax_declaration !== "function") throw new Error("当前桌面程序未包含申报服务，请重新启动更新后的程序。");
        const info = await bridge.get_tax_declaration_info();
        if (!info?.ok) throw new Error(info?.error || info?.message || "无法读取申报默认设置。");
        $("taxWorkbook").value = info.workbook || "";
        $("taxAppPath").value = info.app_path || "";
        $("taxExportStart").value = info.export_start_month || "";
        $("taxExportEnd").value = info.export_end_month || "";
        ready = true;
      } catch (error) {
        showError(error.message || String(error));
      } finally {
        updateControls();
      }
    },
    setRunning(value) {
      running = value;
      updateControls();
    },
    resetResult() {
      outputFile = "";
      exportDir = "";
      $("taxTotal").textContent = "—";
      $("taxSuccessful").textContent = "—";
      $("taxFailed").textContent = "—";
      $("taxSkipped").textContent = "含已有导出跳过";
      $("taxExportFailed").textContent = "另列申报后导出失败";
      updateControls();
    },
    applyResult(result) {
      if (!result || result.total == null) return;
      outputFile = String(result.output_file || "");
      exportDir = String(result.export_dir || "");
      $("outputFile").textContent = outputFile.split(/[\\/]/).pop() || "未生成";
      $("taxTotal").textContent = String(result.total);
      $("taxSuccessful").textContent = String(result.successful ?? 0);
      $("taxFailed").textContent = String(result.failed ?? 0);
      $("taxSkipped").textContent = `已有导出跳过 ${result.skipped ?? 0} 家`;
      $("taxExportFailed").textContent = `另有申报后导出失败 ${result.export_failed ?? 0} 家`;
      if (result.completion_message) $("statusText").textContent = result.completion_message;
      if (result.is_complete === false) {
        $("taskBadge").className = "status-pill failed";
        $("taskBadge").textContent = result.successful > 0 ? "部分完成" : "结果不完整";
      }
      updateControls();
    },
  };

  HQYL.boot(page);
})();
