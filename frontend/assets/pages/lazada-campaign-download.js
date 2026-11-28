(() => {
  const $ = HQYL.$;
  const tool = "lazada_campaign_download";
  let savedLocation = "";
  let processedLocation = "";
  let siteAliases = [];

  function showThreshold(ratio) {
    $("discountThreshold").value = String(Number((Number(ratio) * 100).toFixed(2)));
  }

  function updateRepriceHint() {
    $("repriceHint").hidden = $("processingMode").value !== "reprice_equal_prices";
  }

  function updateSiteHint() {
    const seen = new Set();
    const counts = new Map();
    const issues = [];
    $("store").value.split(/\r?\n/).forEach((line, index) => {
      const name = line.trim();
      const key = name.toLowerCase();
      if (!key || seen.has(key)) return;
      seen.add(key);
      const sites = new Set(siteAliases.filter(([alias]) => name.includes(alias)).map(([, code]) => code));
      if (sites.size !== 1) {
        const reason = sites.size ? "包含多个站点名称" : "未识别到国家名称";
        issues.push(`第 ${index + 1} 行「${name}」${reason}`);
        return;
      }
      const [code] = sites;
      counts.set(code, (counts.get(code) || 0) + 1);
    });
    const summary = Array.from(counts, ([code, count]) => {
      const [country] = siteAliases.find(([, site]) => site === code);
      return `${country}（${code}）${count} 家`;
    }).join("、");
    const hint = [summary && `目标站点　${summary}`, ...issues].filter(Boolean).join("。");
    $("siteHint").textContent = hint || "支持泰国、菲律宾、马来西亚、印尼、越南和新加坡。";
    $("store").setCustomValidity(issues[0] || (seen.size ? "" : "请每行填写一个紫鸟店铺名称"));
  }

  async function download() {
    if (!$("downloadForm").reportValidity()) return;
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", download);
      return;
    }
    const request = {
      account_id: account.id,
      store_names: $("store").value,
      campaign: $("campaign").value.trim(),
      mode: $("mode").value,
      scene: $("scene").value.trim(),
      processing_mode: $("processingMode").value,
      threshold: String(Number(($("discountThreshold").valueAsNumber / 100).toFixed(4))),
      output_dir: $("outputDir").value.trim(),
      timeout_minutes: $("timeoutMinutes").valueAsNumber,
    };
    await HQYL.startTask(() => HQYL.api().start_lazada_campaign_download(request));
  }

  async function chooseDirectory() {
    try {
      const result = await HQYL.api().choose_output_dir($("outputDir").value, false);
      if (result.ok) $("outputDir").value = result.path;
      else if (!result.cancelled) HQYL.showToast(result.error || "选择目录失败");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  HQYL.boot({
    key: tool,
    taskKeys: [tool],
    title: "Lazada 活动模板下载",
    taskContextFilter(context) {
      return Boolean(context.account_id) && context.account_id === HQYL.activeAccount("ziniao")?.id;
    },
    async init() {
      const info = await HQYL.api().get_lazada_campaign_download_info();
      if (!info.ok) throw new Error(info.error || "加载下载设置失败");
      siteAliases = Object.entries(info.site_aliases);
      $("store").addEventListener("input", updateSiteHint);
      updateSiteHint();
      $("processingMode").addEventListener("change", updateRepriceHint);
      updateRepriceHint();
      $("outputDir").value = info.output_dir;
      $("timeoutMinutes").value = info.timeout_minutes;
      showThreshold(info.default_threshold ?? "0.95");
      $("chooseDirBtn").addEventListener("click", chooseDirectory);
      $("openFolderBtn").addEventListener("click", () => HQYL.openOutput(savedLocation));
      $("openProcessedBtn").addEventListener("click", () => HQYL.openOutput(processedLocation));
      $("downloadForm").addEventListener("submit", (event) => {
        event.preventDefault();
        download();
      });
      $("downloadBtn").disabled = false;
    },
    setRunning(running) {
      for (const element of $("downloadForm").elements) {
        if (element !== $("openFolderBtn") && element !== $("openProcessedBtn")) element.disabled = running;
      }
    },
    resetResult() {
      savedLocation = "";
      processedLocation = "";
      $("openFolderBtn").disabled = true;
      $("openProcessedBtn").disabled = true;
    },
    applyResult(result) {
      savedLocation = result.output_file || result.output_dir || "";
      processedLocation = result.processing?.output_dir || "";
      $("openFolderBtn").disabled = !savedLocation;
      $("openProcessedBtn").disabled = !processedLocation;
    },
    onTaskRestored({ context }) {
      $("store").value = context.stores?.map((store) => store.name).join("\n") ?? context.store ?? "";
      updateSiteHint();
      $("campaign").value = context.campaign;
      $("mode").value = context.mode || "invited";
      $("processingMode").value = context.processing_mode ?? "auto";
      updateRepriceHint();
      showThreshold(context.threshold ?? "0.95");
      $("scene").value = context.scene;
      $("outputDir").value = context.output_dir;
      $("timeoutMinutes").value = context.timeout_minutes;
    },
  });
})();
