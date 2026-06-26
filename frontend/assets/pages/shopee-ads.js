(() => {
  const $ = HQYL.$;
  let outputDir = "";

  function storeCount() {
    return $("shopeeAdsStores").value.split(/\r?\n/).filter((line) => line.trim()).length;
  }

  function updateStoreCount() {
    $("skuCount").textContent = String(storeCount());
  }

  function updateStoreInputHint() {
    $("storeInputHint").textContent = $("shopeeAdsMode").value === "custom" ? "每行一个：店铺名称 金额" : "每行一个店铺名称";
  }

  function renderSites(sites) {
    $("shopeeAdsSite").textContent = "";
    sites.forEach((site) => {
      const option = document.createElement("option");
      option.value = site.code;
      option.textContent = `${site.name}（${site.label}）`;
      $("shopeeAdsSite").appendChild(option);
    });
  }

  function payload(account) {
    const company = account && account.extra ? account.extra.company || "" : "";
    return {
      account_id: account ? account.id : "",
      site_code: $("shopeeAdsSite").value,
      company,
      username: account ? account.username : "",
      password: "",
      recharge_mode: $("shopeeAdsMode").value,
      store_names: $("shopeeAdsStores").value,
      max_concurrent_stores: $("shopeeAdsConcurrent").value,
      payment_wait_seconds: $("shopeeAdsPaymentWait").value,
      browser_window_mode: $("shopeeAdsWindowMode").value,
      sync_database: $("shopeeAdsSyncDb").checked,
      client_path: $("shopeeAdsClientPath").value,
      webdriver_path: $("shopeeAdsDriverPath").value,
    };
  }

  async function runRecharge() {
    const account = HQYL.activeAccount("ziniao");
    if (!account) {
      HQYL.openAccountDialog("ziniao", runRecharge);
      return;
    }
    await HQYL.startTask(() => {
      HQYL.appendLog("准备启动 Shopee 广告充值");
      return HQYL.api().start_shopee_ads_recharge(payload(account));
    });
  }

  async function saveConfig() {
    try {
      const result = await HQYL.api().save_shopee_ads_config(payload(HQYL.activeAccount("ziniao")));
      if (!result.ok) throw new Error(result.error || "配置保存失败");
      HQYL.showToast("充值配置已保存");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function refreshPaths(showMessage = true) {
    try {
      const result = await HQYL.api().get_shopee_ads_info();
      if (!result.ok) throw new Error(result.error || "运行路径加载失败");
      renderSites(result.sites || []);
      $("shopeeAdsClientPath").value = result.client_path || "";
      $("shopeeAdsDriverPath").value = result.webdriver_path || "";
      if (showMessage) HQYL.showToast("路径已刷新");
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  async function choosePath(method, targetId, failureMessage) {
    try {
      const result = await HQYL.api()[method]();
      if (result.ok) $(targetId).value = result.path;
      else if (!result.cancelled) throw new Error(result.error || failureMessage);
    } catch (error) {
      HQYL.showToast(error.message || String(error));
    }
  }

  const page = {
    key: "shopee_ads",
    taskKey: "shopee_ads",
    title: "Shopee 广告充值",
    async init(info) {
      outputDir = (info.settings || {}).output_dir || "";
      await refreshPaths(false);
      $("shopeeAdsForm").addEventListener("submit", (event) => { event.preventDefault(); runRecharge(); });
      $("shopeeAdsMode").addEventListener("change", updateStoreInputHint);
      $("shopeeAdsStores").addEventListener("input", updateStoreCount);
      $("shopeeAdsSaveBtn").addEventListener("click", saveConfig);
      $("shopeeAdsRefreshBtn").addEventListener("click", () => refreshPaths(true));
      $("chooseClientPathBtn").addEventListener("click", () => choosePath("choose_client_path", "shopeeAdsClientPath", "选择客户端失败"));
      $("chooseDriverPathBtn").addEventListener("click", () => choosePath("choose_driver_path", "shopeeAdsDriverPath", "选择驱动目录失败"));
      $("shopeeAdsOpenLogBtn").addEventListener("click", () => HQYL.openOutput(outputDir));
      updateStoreInputHint();
      updateStoreCount();
    },
    setRunning(running) {
      ["shopeeAdsRunBtn", "shopeeAdsSaveBtn", "shopeeAdsRefreshBtn", "chooseClientPathBtn", "chooseDriverPathBtn"].forEach((id) => { $(id).disabled = running; });
    },
    resetResult() {
      updateStoreCount();
      $("shopeeAdsOpenLogBtn").disabled = true;
    },
    applyResult(result) {
      $("skuCount").textContent = String(result.matched_store_count ?? storeCount());
      $("recordCount").textContent = String(result.processed_store_count ?? 0);
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = result.output_file || (outputDir ? "日志已生成" : "未生成");
      $("shopeeAdsOpenLogBtn").disabled = !outputDir;
    },
  };

  HQYL.boot(page);
})();
