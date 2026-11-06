(() => {
  const $ = HQYL.$;
  let currentOutputRoot = "";
  let latestManifestPath = "";
  let latestOutputDir = "";

  function getStoreNames() {
    const raw = String($("storesInput").value || "");
    const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    return Array.from(new Set(lines));
  }

  function updateCounts() {
    $("inputCount").textContent = String(getStoreNames().length);
  }

  function renderResults(stores) {
    const tbody = $("resultBody");
    tbody.replaceChildren();

    if (!Array.isArray(stores) || stores.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="9">暂无店铺结果</td>';
      tbody.appendChild(tr);
      return;
    }

    let successCount = 0;
    let failCount = 0;

    stores.forEach((st) => {
      const isSuccess = st.status === "success";
      if (isSuccess) successCount += 1;
      else failCount += 1;

      const tr = document.createElement("tr");
      const statusClass = isSuccess ? "status-tag success" : "status-tag failed";
      const statusText = ({success:"成功", partial:"部分完成", pending:"待处理", running:"采集中", blocked:"已阻塞", cancelled:"已取消", failed:"失败"})[st.status] || "待处理";

      tr.innerHTML = `
        <td><strong>${escapeHtml(st.store_name || st.requested_name || "—")}</strong></td>
        <td>${escapeHtml(st.ads_spend != null ? String(st.ads_spend) : "—")}</td>
        <td>${escapeHtml(st.affiliate_spend != null ? String(st.affiliate_spend) : "—")}</td>
        <td>${escapeHtml(st.coupon_amount != null ? String(st.coupon_amount) : "—")}</td>
        <td><strong style="color: ${isSuccess ? "var(--accent-green, #10b981)" : "inherit"};">${escapeHtml(st.actual_spend != null ? String(st.actual_spend) : "—")}</strong></td>
        <td>${escapeHtml(st.country || "—")}</td>
        <td>${escapeHtml(st.currency || "—")}</td>
        <td><span class="${statusClass}">${statusText}</span></td>
        <td><small>${escapeHtml(st.note || "")}</small></td>
      `;
      tbody.appendChild(tr);
    });

    $("successCount").textContent = String(successCount);
    $("failureBadge").textContent = failCount > 0 ? `未完成 ${failCount}` : "全部成功";
    if (failCount > 0 && latestManifestPath) {
      $("retryBtn").style.display = "inline-block";
    } else {
      $("retryBtn").style.display = "none";
    }
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  async function loadInitialInfo() {
    try {
      const res = await HQYL.api().get_lazada_ads_monthly_info();
      if (!res.ok) throw new Error(res.error || "初始化配置加载失败");

      $("monthInput").value = res.default_month || "";
      $("monthInput").max = res.default_month || "";
      currentOutputRoot = res.default_output_dir || "";
      $("outputDirInput").value = currentOutputRoot;

      const cli = res.cli_status || {};
      if (cli.available) {
        $("cliProfileText").textContent = cli.profile || "已连接 (默认)";
        $("cliStatusBadge").className = "status-pill success";
        $("cliStatusBadge").textContent = "就绪";
      } else {
        $("cliProfileText").textContent = cli.error || "未连接";
        $("cliStatusBadge").className = "status-pill failed";
        $("cliStatusBadge").textContent = "异常";
      }
    } catch (err) {
      HQYL.showToast(err.message || String(err));
    }
  }

  async function handlePreview() {
    const stores = getStoreNames();
    if (!stores.length) {
      HQYL.showToast("请输入至少一个店铺名称");
      return;
    }
    try {
      HQYL.appendLog("正在进行紫鸟店铺匹配与预检...");
      const payload = {
        month: $("monthInput").value,
        country: $("countrySelect").value,
        store_names: stores,
        output_root: $("outputDirInput").value,
      };
      const res = await HQYL.api().preview_lazada_ads_monthly(payload);
      $("matchedCount").textContent = String(res.matched_count || 0);
      if (res.issues && res.issues.length > 0) {
        HQYL.showToast(`存在 ${res.issues.length} 个店铺匹配问题，请查看日志`);
        res.issues.forEach((iss) => {
          HQYL.appendLog(`[预检警告] ${iss.requested_name}：${iss.error}`);
        });
      } else {
        if (!res.ok) throw new Error(res.error || "预检失败");
        HQYL.showToast(`店铺预检通过，已唯一匹配 ${res.matched_count} 家店铺`);
        HQYL.appendLog(`[预检成功] ${res.matched_count} 家店铺均已唯一匹配`);
      }
    } catch (err) {
      HQYL.showToast(err.message || String(err));
      HQYL.appendLog(`[预检异常] ${err.message}`);
    }
  }

  async function handleStart() {
    const stores = getStoreNames();
    if (!stores.length) {
      HQYL.showToast("请输入至少一个店铺名称");
      return;
    }
    if (!$("monthInput").value) {
      HQYL.showToast("请选择自然月份");
      return;
    }

    const payload = {
      month: $("monthInput").value,
      country: $("countrySelect").value,
      store_names: stores,
      output_root: $("outputDirInput").value,
    };

    await HQYL.startTask(
      () => {
        HQYL.appendLog(`开始执行 Lazada 广告月报采集：月份 ${payload.month}，店铺数 ${stores.length}`);
        return HQYL.api().start_lazada_ads_monthly(payload);
      }
    );
  }

  async function handleRetry() {
    if (!latestManifestPath) {
      HQYL.showToast("暂无可重试的批次");
      return;
    }
    const retryManifest = latestManifestPath;
    await HQYL.startTask(
      () => {
        HQYL.appendLog(`重试未完成店铺，批次清单: ${retryManifest}`);
        return HQYL.api().retry_lazada_ads_monthly({ manifest_path: retryManifest });
      }
    );
  }

  async function handleOpenOutput() {
    if (latestOutputDir && HQYL.api().open_path) {
      const result = await HQYL.api().open_path(latestOutputDir);
      if (!result.ok) HQYL.showToast(result.error || "打开目录失败");
    } else {
      HQYL.showToast("未找到输出目录");
    }
  }

  const page = {
    key: "lazada_ads_monthly",
    title: "Lazada 广告月报",
    taskKey: "lazada_ads_monthly",
    async init() {
      $("storesInput").addEventListener("input", updateCounts);
      $("previewBtn").addEventListener("click", handlePreview);
      $("adsMonthlyForm").addEventListener("submit", (e) => {
        e.preventDefault();
        handleStart();
      });
      $("retryBtn").addEventListener("click", handleRetry);
      $("openOutputBtn").addEventListener("click", handleOpenOutput);
      $("chooseOutputBtn").addEventListener("click", async () => {
        try {
          const result = await HQYL.api().choose_output_dir($("outputDirInput").value);
          if (result.ok) $("outputDirInput").value = result.path;
          else if (!result.cancelled) throw new Error(result.error || "选择目录失败");
        } catch (error) { HQYL.showToast(error.message || String(error)); }
      });

      await loadInitialInfo();
    },
    resetResult() {
      latestManifestPath = "";
      latestOutputDir = "";
      $("matchedCount").textContent = "0";
      $("successCount").textContent = "0";
      $("outputFile").textContent = "未生成";
      $("openOutputBtn").disabled = true;
      $("retryBtn").style.display = "none";
      $("failureBadge").textContent = "未就绪";
      renderResults([]);
      updateCounts();
    },
    onTaskUpdate(task) {
      const res = task.result || {};
      if (res.workbook_path) {
        $("outputFile").textContent = res.workbook_path.split(/[\\/]/).pop();
        latestOutputDir = res.batch_dir || "";
        $("openOutputBtn").disabled = false;
      }
      if (res.manifest_path) {
        latestManifestPath = res.manifest_path;
      }
      if (Array.isArray(res.stores)) {
        renderResults(res.stores);
      }
    },
  };

  HQYL.boot(page);
})();
