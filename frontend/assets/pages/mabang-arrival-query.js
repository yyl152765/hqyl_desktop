(() => {
  const $ = HQYL.$;
  let outputRoot = "";
  let resultOutputDir = "";
  let outputFiles = [];

  function basename(path) {
    return String(path || "").split(/[\\/]/).pop() || "";
  }

  function remarkItems() {
    const seen = new Set();
    return $("arrivalRemarkText").value
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter((item) => {
        if (!item || seen.has(item)) return false;
        seen.add(item);
        return true;
      });
  }

  function updateRemarkCount() {
    $("remarkCount").textContent = String(remarkItems().length);
  }

  async function chooseOutputDir() {
    const result = await HQYL.api().choose_output_dir($("arrivalOutputDir").value.trim() || outputRoot);
    if (result.ok) {
      outputRoot = result.path;
      $("arrivalOutputDir").value = result.path;
    } else if (!result.cancelled) {
      HQYL.showToast(result.error || "选择目录失败");
    }
  }

  async function runArrivalQuery() {
    const remarks = remarkItems();
    if (!remarks.length) {
      HQYL.showToast("请至少输入一条调拨备注");
      return;
    }
    const selectedOutputRoot = $("arrivalOutputDir").value.trim() || outputRoot;
    if (!selectedOutputRoot) {
      HQYL.showToast("请选择输出目录");
      return;
    }
    outputRoot = selectedOutputRoot;
    const account = HQYL.activeAccount("mabang");
    if (!account) {
      HQYL.openAccountDialog("mabang", runArrivalQuery);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog(`准备按 ${remarks.length} 条备注查询调拨批次`);
      HQYL.appendLog("固定规则：状态全部、搜索备注、每条备注独立导出、关闭合并共有项");
      return HQYL.api().start_mabang_arrival_query({
        account_id: account.id,
        remarks,
        output_dir: selectedOutputRoot,
      });
    });
  }

  const page = {
    key: "mabang_arrival_query",
    taskKeys: ["mabang_arrival_query"],
    title: "马帮到货查询",
    taskContextFilter(context) {
      const account = HQYL.activeAccount("mabang");
      return !context.account_id || String(context.account_id) === String(account?.id || "");
    },
    async init(info) {
      outputRoot = info.settings?.output_dir || "";
      $("arrivalOutputDir").value = outputRoot;
      $("arrivalQueryForm").addEventListener("submit", (event) => {
        event.preventDefault();
        runArrivalQuery();
      });
      $("arrivalRemarkText").addEventListener("input", updateRemarkCount);
      $("clearRemarksBtn").addEventListener("click", () => {
        $("arrivalRemarkText").value = "";
        updateRemarkCount();
      });
      $("arrivalChooseDirBtn").addEventListener("click", chooseOutputDir);
      $("arrivalOpenOutputBtn").addEventListener("click", () => HQYL.openOutput(resultOutputDir || outputFiles[0] || outputRoot));
      updateRemarkCount();
    },
    setRunning(running) {
      $("arrivalRunBtn").disabled = running;
      $("clearRemarksBtn").disabled = running;
      $("arrivalRemarkText").disabled = running;
      $("arrivalOutputDir").disabled = running;
      $("arrivalChooseDirBtn").disabled = running;
    },
    resetResult() {
      outputFiles = [];
      resultOutputDir = "";
      $("batchCount").textContent = "0";
      $("unmatchedSummary").textContent = "未命中 0 条备注";
      $("outputFile").textContent = "未生成";
      $("arrivalOutputSummary").textContent = "尚未生成文件；命名规则：到货查询_时间_搜索备注.xls[x]";
      $("arrivalOpenOutputBtn").disabled = true;
      updateRemarkCount();
    },
    onTaskRestored(task) {
      const count = Number(task?.context?.remark_count || 0);
      if (count > 0) $("remarkCount").textContent = String(count);
      const restoredRoot = String(task?.context?.output_root || "");
      if (restoredRoot) {
        outputRoot = restoredRoot;
        $("arrivalOutputDir").value = restoredRoot;
      }
    },
    applyResult(result) {
      const unmatched = Array.isArray(result.unmatched_remarks) ? result.unmatched_remarks : [];
      outputFiles = Array.isArray(result.output_files)
        ? result.output_files.filter(Boolean)
        : result.output_file ? [result.output_file] : [];
      outputRoot = result.output_root || outputRoot;
      resultOutputDir = result.output_dir || "";
      if (outputRoot) $("arrivalOutputDir").value = outputRoot;
      $("remarkCount").textContent = String(result.remark_count ?? remarkItems().length);
      $("batchCount").textContent = String(result.matched_batch_count ?? 0);
      $("unmatchedSummary").textContent = `未命中 ${unmatched.length} 条备注`;
      $("outputFile").textContent = outputFiles.length ? `${outputFiles.length} 个文件` : "未生成";
      const exports = Array.isArray(result.exports) ? result.exports : [];
      const summary = exports.length
        ? exports.map((item) => `${item.remark} → ${basename(item.output_file)}`).join("；")
        : outputFiles.map(basename).join("；");
      $("arrivalOutputSummary").textContent = summary || "未生成文件";
      $("arrivalOpenOutputBtn").disabled = !resultOutputDir && !outputFiles.length;
    },
  };

  HQYL.boot(page);
})();
