const HQYL = (() => {
  const state = {
    page: null,
    info: null,
    accounts: [],
    activeAccountIds: {},
    taskId: "",
    pollTimer: 0,
    outputFile: "",
    outputDir: "",
    accountContinuation: null,
  };

  const $ = (id) => document.getElementById(id);
  const VENDOR_META = {
    mabang: { label: "马帮", title: "马帮 ERP", eyebrow: "MABANG ERP", logo: "M", empty: "尚未绑定马帮账号", color: "" },
    ziniao: { label: "紫鸟", title: "紫鸟账号", eyebrow: "ZINIAO BROWSER", logo: "Z", empty: "尚未绑定紫鸟账号", color: "var(--accent-purple,#7c3aed)" },
    bigseller: { label: "BigSeller", title: "BigSeller 账号", eyebrow: "BIGSELLER", logo: "B", empty: "尚未绑定 BigSeller 账号", color: "var(--accent-orange,#f97316)" },
    echotik: { label: "EchoTik", title: "EchoTik 账号", eyebrow: "ECHOTIK", logo: "E", empty: "尚未绑定 EchoTik 账号", color: "var(--green,#138a72)" },
  };

  function vendorMeta(vendor) {
    return VENDOR_META[vendor] || { label: vendor, title: "业务账号", eyebrow: "ACCOUNT", logo: "?", empty: "尚未绑定账号", color: "" };
  }

  function api() {
    if (window.pywebview && window.pywebview.api) return window.pywebview.api;
    throw new Error("pywebview bridge is not ready");
  }

  const SIDEBAR_STORAGE_KEY = "hqyl.sidebar.v1";
  const SIDEBAR_FAVORITE_LIMIT = 5;
  // One catalog drives the full menu, search results, favorites, and current location.
  const SIDEBAR_GROUPS = [
    { id: "finance", name: "财务流程", icon: "¥", keywords: "财务 对账 Reconciliation", items: [
      { id: "vietnam_income_reconciliation", href: "vietnam-income-reconciliation.html", name: "越南收支报表", icon: "账", tag: "越南", category: "越南 Shopee", description: "紫鸟+马帮数据核对", keywords: "ziniao mabang 收入 支出 对账" },
      { id: "kec_reconciliation", href: "kec-reconciliation.html", name: "KEC 对账", icon: "K", tag: "KEC", category: "海外仓对账", description: "仓租费按实际体积核对", keywords: "kec 仓储 仓租 对账 实际体积 卸货 耗材 退件 杂费" },
    ] },
    { id: "mabang", name: "马帮流程", icon: "M", keywords: "马帮 Mabang ERP", items: [
      { id: "group_sales", href: "group-sales.html", name: "各组销量报表", icon: "销", tag: "菲律宾", category: "菲律宾 Shopee", description: "按小组导出销量", keywords: "销售 商品 小组" },
      { id: "sample_registration", href: "sample-registration.html", name: "网红寄样登记", icon: "样", tag: "TikTok", category: "TikTok", description: "样品订单查询与导出", keywords: "达人 钉钉 同步" },
      { id: "developer_sales_income_summary", href: "developer-sales-income-summary.html", name: "开发与销售收入汇总", icon: "汇", tag: "采购", category: "采购流程", description: "付款日期 · 美元收入", keywords: "开发员 订单金额 报表" },
      { id: "mabang_arrival_query", href: "mabang-arrival-query.html", name: "到货查询", icon: "到", tag: "调拨", category: "仓库调拨", description: "按备注查询调拨并导出", keywords: "仓储 仓库" },
      { id: "mabang_income_expense_report", href: "mabang-income-expense-report.html", name: "收支报表", icon: "收", tag: "财务", category: "通用工具", description: "按分类、海外仓和发货时间导出", keywords: "收入 支出 财务导出" },
      { id: "temu_shipping_channel", href: "temu-shipping-channel.html", name: "TEMU发货渠道更改", icon: "运", tag: "TEMU", category: "物流授权", description: "按 Excel 顺序设置已开启渠道的长宽高和重量", keywords: "物流 发货 渠道 申报 重量" },
      { id: "mabang_warehouse_permission", href: "mabang-warehouse-permission.html", name: "仓库权限开通", icon: "仓", tag: "权限", category: "通用工具", description: "批量添加员工仓库", keywords: "授权 仓储 员工权限" },
      { id: "mabang_developer_permission", href: "mabang-developer-permission.html", name: "批量添加开发员", icon: "开", tag: "权限", category: "通用工具", description: "员工岗位与商品查看设置", keywords: "授权 员工权限" },
      { id: "sku_inventory_query", href: "sku-inventory-query.html", name: "SKU库存与可售天数", icon: "存", tag: "库存", category: "通用工具", description: "库存明细与爆款旺款缺失仓库", keywords: "仓储 SKU仓库库存 开发员 爆款 旺款 东南亚 缺失仓库" },
      { id: "purchase_log", href: "purchase-log.html", name: "SKU 采购日志", icon: "购", tag: "采购", category: "通用工具", description: "查询采购记录", keywords: "采购流程" },
    ] },
    { id: "ziniao", name: "紫鸟流程", icon: "Z", keywords: "紫鸟 ZIniao Superbrowser", items: [
      { id: "temu_on_sale_export", href: "temu-on-sale-export.html", name: "在售商品导出", icon: "导", tag: "TEMU", category: "TEMU 东南亚", description: "选店导出、原文件归档与 Excel 汇总", keywords: "商品管理 SKU 在售中 失败重试" },
      { id: "temu_balance_statistics", href: "temu-balance-statistics.html", name: "余额统计", icon: "余", tag: "TEMU", category: "TEMU 财务", description: "当前账户总金额与指定月份待处理款项", keywords: "资金 月份 余额 财务 截图" },
      { id: "shopee_ads", href: "shopee-ads.html", name: "广告充值", icon: "充", tag: "Shopee", category: "Shopee 广告", description: "多站点店铺充值", keywords: "印尼 泰国 菲律宾 越南 马来西亚 马来 按表充值 营销" },
      { id: "lazada_withdrawal_statistics", href: "lazada-withdrawal-statistics.html", name: "提现统计", icon: "提", tag: "Lazada", category: "Lazada 财务", description: "Excel 与账单归档", keywords: "菲律宾 马来西亚 泰国 收入" },
      { id: "lazada_balance_statistics", href: "lazada-balance-statistics.html", name: "余额统计", icon: "余", tag: "Lazada", category: "Lazada 财务", description: "多站点当前余额与处理中提现截图", keywords: "Income Balance Ads processing 菲律宾 马来西亚 泰国 印尼 越南 新加坡" },
      { id: "lazada_monthly_report", href: "lazada-monthly-report.html", name: "月度账单下载", icon: "月", tag: "Lazada", category: "Lazada 财务", description: "按月份批量下载账单", keywords: "泰国 马来西亚 菲律宾 月报" },
      { id: "lazada_ads_data", href: "lazada-ads-data.html", name: "广告数据", icon: "广", tag: "Lazada", category: "Lazada 广告", description: "指定日期采集广告费与业绩并写入 Sheet", keywords: "泰国 钉钉 广告 支出 业绩 Sheet" },
      { id: "lazada_bill_detail", href: "lazada-bill-detail.html", name: "后台收支数据", icon: "账", tag: "Lazada", category: "Lazada 账单", description: "按国家采集店铺后台收支数据并写入指定 Sheet", keywords: "泰国 菲律宾 马来 印尼 越南 钉钉 总金额 收入 扣减项 截图 账单区间" },
    ] },
    { id: "echotik", name: "EchoTik", icon: "E", keywords: "EchoTik TikTok Data", items: [
      { id: "echotik_collect", href: "echotik-collect.html", name: "商品达人采集", icon: "采", tag: "泰国", category: "泰国商品库", description: "关键词或全库商品与达人导出", keywords: "网红 选品 TikTok" },
    ] },
    { id: "bigseller", name: "BigSeller", icon: "B", keywords: "BigSeller BS Listing Sync", items: [
      { id: "bigseller_sync", href: "bigseller-sync.html", name: "产品/库存同步", icon: "同", tag: "Shopee", category: "Shopee 商品", description: "接口同步在售/售完", keywords: "商品 库存" },
      { id: "bigseller_item_id_query", href: "bigseller-item-id-query.html", name: "BS 商品ID查询", icon: "ID", tag: "Shopee", category: "Shopee 商品", description: "批量 SKU 匹配商品ID", keywords: "子SKU Item ID Fuzzy Search Views" },
      { id: "bigseller_sku_benchmark", href: "bigseller-sku-benchmark.html", name: "滞销SKU爆款对标", icon: "标", tag: "Shopee", category: "Shopee 商品", description: "按浏览量或销量对标店铺", keywords: "选品 Views 销售" },
      { id: "bigseller_claim_query", href: "bigseller-claim-query.html", name: "新品认领时间查询", icon: "新", tag: "Shopee", category: "Shopee 商品", description: "导入 SKU 查询最早创建商品", keywords: "新品 认领 主SKU 创建时间 上架时间 店铺 Excel" },
    ] },
  ];
  const SIDEBAR_MODULES = SIDEBAR_GROUPS.flatMap((group) => group.items.map((item) => ({ ...item, group })));
  let sidebarController = null;
  let sidebarPreferences = null;
  let sidebarNativeLoad = null;
  let sidebarNativeReady = false;
  let sidebarSaveQueue = Promise.resolve();
  let sidebarPendingOperations = [];

  function normalizeSidebarPreferences(value) {
    const uniqueKnown = (values, known, limit) => Array.isArray(values)
      ? [...new Set(values.filter((id) => typeof id === "string" && known.has(id)))].slice(0, limit)
      : [];
    return {
      favorites: uniqueKnown(value?.favorites, new Set(SIDEBAR_MODULES.map((item) => item.id)), SIDEBAR_FAVORITE_LIMIT),
      expandedGroups: uniqueKnown(value?.expandedGroups, new Set(SIDEBAR_GROUPS.map((group) => group.id)), SIDEBAR_GROUPS.length),
    };
  }

  function cacheSidebarPreferences() {
    try { window.localStorage.setItem(SIDEBAR_STORAGE_KEY, JSON.stringify({ ...sidebarPreferences, pendingOperations: sidebarPendingOperations })); } catch (_error) { /* The desktop bridge remains authoritative. */ }
  }

  function validSidebarOperation(operation) {
    return operation?.type === "collapseAll"
      || (operation?.type === "favorite" && typeof operation.enabled === "boolean" && SIDEBAR_MODULES.some((item) => item.id === operation.id))
      || (operation?.type === "group" && typeof operation.enabled === "boolean" && SIDEBAR_GROUPS.some((group) => group.id === operation.id));
  }

  function applySidebarOperation(operation) {
    if (operation.type === "collapseAll") { sidebarPreferences.expandedGroups = []; return true; }
    const values = operation.type === "favorite" ? sidebarPreferences.favorites : sidebarPreferences.expandedGroups;
    const index = values.indexOf(operation.id);
    if (!operation.enabled && index >= 0) values.splice(index, 1);
    else if (operation.enabled && index < 0) {
      if (operation.type === "favorite" && values.length >= SIDEBAR_FAVORITE_LIMIT) return false;
      values.push(operation.id);
    }
    return true;
  }

  function queueNativeSidebarSave() {
    const bridge = window.pywebview?.api;
    if (!sidebarNativeReady || typeof bridge?.save_sidebar_preferences !== "function") return;
    sidebarSaveQueue = sidebarSaveQueue.then(async () => {
      const result = await bridge.save_sidebar_preferences(normalizeSidebarPreferences(sidebarPreferences));
      if (!result.ok) throw new Error(result.error || "导航偏好保存失败");
    }).catch(() => {
      showToast("导航偏好未能保存，重启后可能无法恢复，请稍后重试");
    });
  }

  function loadSidebarPreferences() {
    const bridge = window.pywebview?.api;
    if (sidebarNativeLoad || typeof bridge?.get_sidebar_preferences !== "function") return;
    sidebarNativeLoad = (async () => {
      try {
        const result = await bridge.get_sidebar_preferences();
        if (!result.ok) throw new Error(result.error || "无法读取导航偏好");
        const pending = sidebarPendingOperations;
        if (result.preferences !== null && result.preferences !== undefined) {
          sidebarPreferences = normalizeSidebarPreferences(result.preferences);
        }
        // Replay intents on the saved baseline; an early favorite must not erase older favorites.
        // With no native file, the cached state already includes every pending operation.
        const outcomes = result.preferences ? pending.map(applySidebarOperation) : [];
        sidebarController?.revealCurrentGroup(pending);
        sidebarNativeReady = true;
        sidebarPendingOperations = [];
        cacheSidebarPreferences();
        sidebarController?.update();
        if (!pending.length) sidebarController?.scrollToCurrent();
        if (pending.length) queueNativeSidebarSave();
        if (outcomes.includes(false)) showToast("已恢复原有常用模块，最多固定 5 个，请先取消一个再添加");
      } catch (_error) {
        sidebarNativeLoad = null;
        showToast("导航偏好读取失败，暂时使用本次会话设置");
      }
    })();
  }

  function saveSidebarPreferences(operation) {
    if (!sidebarNativeReady) {
      // Keep pending intents across an early page change before pywebviewready.
      // Keep removals in order: they may free a slot for a later addition at the five-item limit.
      sidebarPendingOperations.push(operation);
    }
    cacheSidebarPreferences();
    if (sidebarNativeReady) queueNativeSidebarSave(); else loadSidebarPreferences();
  }

  function renderSidebar(activePage) {
    const sidebar = $("appSidebar");
    if (!sidebar || sidebarController?.element === sidebar) return;
    if (!sidebarPreferences) {
      try {
        const cached = JSON.parse(window.localStorage.getItem(SIDEBAR_STORAGE_KEY));
        sidebarPreferences = normalizeSidebarPreferences(cached);
        sidebarPendingOperations = Array.isArray(cached?.pendingOperations) ? cached.pendingOperations.filter(validSidebarOperation) : [];
      }
      catch (_error) { sidebarPreferences = normalizeSidebarPreferences(null); }
    }
    const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
    const current = SIDEBAR_MODULES.find((item) => item.id === activePage);
    const currentPath = current ? `${current.group.name} / ${current.name}` : activePage === "settings" ? "设置" : "工作台";
    sidebar.classList.add("sidebar-compact");
    sidebar.innerHTML = `
      <a class="brand" href="dashboard.html" aria-label="返回工作台">
        <div class="brand-mark"><img src="../assets/logo.png" alt="寰球云联" /></div>
        <div class="brand-copy"><div class="brand-title">寰球云联</div><div class="brand-subtitle">自动化工作台</div></div>
      </a>
      <div class="nav-toolbar">
        <label class="nav-search" for="navSearch"><span class="nav-search-icon" aria-hidden="true">⌕</span><input id="navSearch" type="text" role="searchbox" enterkeyhint="search" placeholder="搜索模块 / 平台" aria-label="搜索模块、平台或用途" aria-keyshortcuts="Control+K Meta+K" autocomplete="off" /><kbd class="nav-search-shortcut">Ctrl K</kbd><button class="nav-search-clear" type="button" aria-label="清空模块搜索" hidden>×</button></label>
        <a class="nav-settings nav-home${activePage === "dashboard" ? " active" : ""}" data-page="dashboard" href="dashboard.html"${activePage === "dashboard" ? ' aria-current="page"' : ""}><span class="nav-leaf-icon" aria-hidden="true">首</span><span class="nav-leaf-copy"><strong>工作台</strong></span></a>
      </div>
      <nav class="nav-tree" aria-label="流程导航">
        <section class="nav-favorites" id="navFavorites" aria-label="我的常用" hidden><div class="nav-list-heading"><span>我的常用</span><span class="nav-list-count" id="navFavoriteCount"></span></div><div id="navFavoriteList"></div></section>
        <div class="nav-list-heading nav-browse-heading"><span id="navListTitle">全部模块</span><button type="button" class="nav-collapse-all" aria-label="收起全部分组">全部收起</button></div>
        <div id="navGroups"></div>
        <div id="navSearchResults" hidden></div>
        <div class="nav-empty-state" id="navSearchEmpty" hidden>没有找到模块，试试名称、平台或用途。</div>
      </nav>
      <div class="nav-footer">
        <div class="nav-location" title="${escape(currentPath)}" aria-label="当前位置：${escape(currentPath)}">${escape(currentPath)}</div>
        <a class="nav-settings${activePage === "settings" ? " active" : ""}" data-page="settings" href="settings.html"${activePage === "settings" ? ' aria-current="page"' : ""}><span class="nav-leaf-icon" aria-hidden="true">⚙</span><span class="nav-leaf-copy"><strong>设置</strong></span></a>
        <div class="side-status"><div class="side-status-head"><div class="side-label">当前版本</div><span class="side-channel">稳定版</span></div><div class="side-version-row"><span class="side-version-prefix">v</span><div id="appVersion" class="side-version">—</div></div><div id="updateSummary" class="side-hint">模块按需加载</div></div>
      </div>
      <div class="sr-only" id="navAnnouncement" role="status" aria-live="polite"></div>`;
    const search = $("navSearch");
    const tree = sidebar.querySelector(".nav-tree");
    let composing = false;
    let browseScrollTop = 0;
    let wasSearching = false;
    const queryTokens = () => search.value.normalize("NFKC").trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches = () => {
      const tokens = queryTokens();
      return SIDEBAR_MODULES.filter((item) => {
        const text = [item.name, item.category, item.description, item.tag, item.keywords, item.group.name, item.group.keywords].join(" ").normalize("NFKC").toLowerCase();
        return tokens.every((token) => text.includes(token));
      });
    };
    function renderRow(item, searchResult = false) {
      const selected = item.id === activePage;
      const favorite = sidebarPreferences.favorites.includes(item.id);
      const path = `${item.group.name} / ${item.category}`;
      const title = `${item.name}\n${path}\n${item.description}`;
      return `<div class="nav-row${selected ? " active" : ""}${searchResult ? " nav-search-result" : ""}"><a class="nav-leaf${selected ? " active" : ""}" data-page="${item.id}" href="${item.href}" title="${escape(title)}" aria-label="${escape(`${item.name}，${path}，${item.description}`)}"${selected ? ' aria-current="page"' : ""}><span class="nav-leaf-icon" aria-hidden="true">${escape(item.icon)}</span><span class="nav-leaf-copy"><strong>${escape(item.name)}</strong>${searchResult ? `<small class="nav-result-path">${escape(path)}</small>` : ""}</span>${searchResult ? "" : `<span class="nav-item-tag">${escape(item.tag)}</span>`}</a><button type="button" class="nav-favorite-toggle" data-nav-favorite="${item.id}" aria-pressed="${favorite}" aria-label="${favorite ? "取消常用" : "设为常用"}：${escape(item.name)}" title="${favorite ? "取消常用" : "设为常用（最多 5 个）"}">${favorite ? "★" : "☆"}</button></div>`;
    }
    function update() {
      const focused = document.activeElement;
      const focusedFavorite = focused?.dataset?.navFavorite;
      const focusedGroup = focused?.dataset?.navGroup;
      const focusedContainer = focusedFavorite ? focused.closest("#navFavoriteList, #navSearchResults, #navGroups")?.id : null;
      const searching = queryTokens().length > 0;
      if (searching && !wasSearching) browseScrollTop = tree.scrollTop;
      const previousScrollTop = tree.scrollTop;
      $("navGroups").innerHTML = SIDEBAR_GROUPS.map((group) => {
        const expanded = sidebarPreferences.expandedGroups.includes(group.id);
        const groupItems = SIDEBAR_MODULES.filter((item) => item.group.id === group.id);
        return `<section class="nav-section"><button type="button" class="nav-group-title${current?.group.id === group.id ? " has-current" : ""}" data-nav-group="${group.id}" aria-expanded="${expanded}" aria-controls="navGroup-${group.id}"><span class="nav-icon" aria-hidden="true">${escape(group.icon)}</span><span class="nav-group-copy"><strong>${escape(group.name)}</strong></span><span class="nav-group-count" aria-label="${group.items.length} 个模块">${group.items.length}</span><span class="nav-chevron" aria-hidden="true">›</span></button><div class="nav-group-items" id="navGroup-${group.id}"${expanded ? "" : " hidden"}>${groupItems.map((item) => renderRow(item)).join("")}</div></section>`;
      }).join("");
      $("navFavoriteList").innerHTML = sidebarPreferences.favorites.map((id) => renderRow(SIDEBAR_MODULES.find((item) => item.id === id))).join("");
      $("navFavoriteCount").textContent = `${sidebarPreferences.favorites.length} / ${SIDEBAR_FAVORITE_LIMIT}`;
      $("navFavorites").hidden = searching || sidebarPreferences.favorites.length === 0;
      $("navGroups").hidden = searching;
      $("navSearchResults").hidden = !searching;
      const results = searching ? matches() : [];
      $("navSearchResults").innerHTML = results.map((item) => renderRow(item, true)).join("");
      $("navSearchEmpty").hidden = !searching || results.length > 0;
      $("navListTitle").textContent = searching ? `搜索结果 · ${results.length}` : `全部模块 · ${SIDEBAR_MODULES.length}`;
      sidebar.querySelector(".nav-collapse-all").hidden = searching || sidebarPreferences.expandedGroups.length === 0;
      sidebar.querySelector(".nav-search-clear").hidden = search.value.length === 0;
      sidebar.querySelector(".nav-search-shortcut").hidden = search.value.length > 0;
      tree.scrollTop = searching ? (wasSearching ? previousScrollTop : 0) : (wasSearching ? browseScrollTop : previousScrollTop);
      wasSearching = searching;
      if (searching) $("navAnnouncement").textContent = `找到 ${results.length} 个模块`;
      const focusReplacement = focusedFavorite && focusedContainer
        ? $(focusedContainer).querySelector(`[data-nav-favorite="${focusedFavorite}"]`)
        : focusedGroup ? sidebar.querySelector(`[data-nav-group="${focusedGroup}"]`) : null;
      if (focusReplacement?.getClientRects().length) focusReplacement.focus({ preventScroll: true });
      else if (focusedFavorite || focusedGroup) search.focus({ preventScroll: true });
    }
    function revealCurrentGroup(operations = []) {
      const choice = [...operations].reverse().find((operation) => operation.type === "collapseAll" || (operation.type === "group" && operation.id === current?.group.id));
      if (choice && (choice.type === "collapseAll" || !choice.enabled)) return;
      if (current && !sidebarPreferences.expandedGroups.includes(current.group.id)) sidebarPreferences.expandedGroups.push(current.group.id);
    }
    function scrollToCurrent() {
      if (queryTokens().length) return;
      const link = $("navGroups").querySelector('[aria-current="page"]');
      if (!link || !link.getClientRects().length) return;
      const rect = link.getBoundingClientRect();
      const viewport = tree.getBoundingClientRect();
      if (rect.bottom > viewport.bottom) tree.scrollTop += rect.bottom - viewport.bottom + 4;
      else if (rect.top < viewport.top) tree.scrollTop -= viewport.top - rect.top + 4;
    }
    function clearSearch() {
      search.value = "";
      update();
      search.focus();
      $("navAnnouncement").textContent = "已清空搜索，恢复模块分组";
    }
    sidebarController = { element: sidebar, update, revealCurrentGroup, scrollToCurrent };
    revealCurrentGroup();
    if (current && sidebarPendingOperations.length) {
      // Entering a new page reveals its group after earlier-page intents have been replayed.
      sidebarPendingOperations.push({ type: "group", id: current.group.id, enabled: true });
      cacheSidebarPreferences();
    }
    update();
    window.requestAnimationFrame(scrollToCurrent);
    search.addEventListener("input", update);
    search.addEventListener("compositionstart", () => { composing = true; });
    search.addEventListener("compositionend", () => { composing = false; update(); });
    search.addEventListener("keydown", (event) => {
      if (composing || event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") { event.preventDefault(); clearSearch(); }
      else if (event.key === "Enter" && queryTokens().length) {
        event.preventDefault();
        $("navSearchResults").querySelector("a[data-page]")?.click();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (!composing && !event.isComposing && event.keyCode !== 229 && !event.altKey && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); search.focus(); search.select();
      }
    });
    sidebar.addEventListener("click", async (event) => {
      const favorite = event.target.closest("[data-nav-favorite]");
      if (favorite) {
        const id = favorite.dataset.navFavorite;
        const containerId = favorite.closest("#navFavoriteList, #navSearchResults, #navGroups").id;
        const index = sidebarPreferences.favorites.indexOf(id);
        if (index >= 0) sidebarPreferences.favorites.splice(index, 1);
        else if (sidebarPreferences.favorites.length < SIDEBAR_FAVORITE_LIMIT) sidebarPreferences.favorites.push(id);
        else {
          const message = "最多固定 5 个常用模块，请先取消一个";
          $("navAnnouncement").textContent = message; showToast(message); return;
        }
        saveSidebarPreferences({ type: "favorite", id, enabled: index < 0 });
        update();
        const nextFocus = $(containerId).querySelector(`[data-nav-favorite="${id}"]`);
        if (nextFocus?.getClientRects().length) nextFocus.focus({ preventScroll: true }); else search.focus();
        $("navAnnouncement").textContent = index >= 0 ? "已取消常用" : "已添加到我的常用";
        return;
      }
      const group = event.target.closest("[data-nav-group]");
      if (group) {
        const id = group.dataset.navGroup;
        const index = sidebarPreferences.expandedGroups.indexOf(id);
        if (index >= 0) sidebarPreferences.expandedGroups.splice(index, 1); else sidebarPreferences.expandedGroups.push(id);
        saveSidebarPreferences({ type: "group", id, enabled: index < 0 });
        update();
        sidebar.querySelector(`[data-nav-group="${id}"]`).focus({ preventScroll: true });
        return;
      }
      if (event.target.closest(".nav-search-clear")) { clearSearch(); return; }
      if (event.target.closest(".nav-collapse-all")) {
        sidebarPreferences.expandedGroups = [];
        saveSidebarPreferences({ type: "collapseAll" });
        update();
        sidebar.querySelector("[data-nav-group]").focus({ preventScroll: true });
        return;
      }
      const link = event.target.closest("a[href]");
      if (link && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
        event.preventDefault();
        const destination = SIDEBAR_MODULES.find((item) => item.id === link.dataset.page);
        if (destination && !sidebarPreferences.expandedGroups.includes(destination.group.id)) {
          sidebarPreferences.expandedGroups.push(destination.group.id);
          saveSidebarPreferences({ type: "group", id: destination.group.id, enabled: true });
        }
        // Finish native preference writes before unloading the document.
        await sidebarNativeLoad;
        let pendingSave;
        do {
          pendingSave = sidebarSaveQueue;
          await pendingSave;
        } while (pendingSave !== sidebarSaveQueue);
        window.location.href = link.href;
      }
    });
  }


  function vendorAccounts(vendor) {
    return state.accounts.filter((account) => account.vendor === vendor);
  }

  function activeAccount(vendor) {
    const activeId = state.activeAccountIds[vendor] || "";
    return state.accounts.find((account) => account.vendor === vendor && account.id === activeId) || null;
  }

  function applyAccountState(accountState) {
    state.accounts = Array.isArray(accountState.accounts) ? accountState.accounts : [];
    state.activeAccountIds = accountState.active_account_ids || {};
    ["mabang", "ziniao", "bigseller", "echotik"].forEach((vendor) => {
      const account = activeAccount(vendor);
      const meta = vendorMeta(vendor);
      document.querySelectorAll(`[data-active-account-name][data-vendor="${vendor}"]`).forEach((element) => {
        if (!account) {
          element.textContent = meta.empty;
          return;
        }
        const company = vendor === "ziniao" && account.extra ? account.extra.company : "";
        element.textContent = `${company || account.name} · ${account.username}`;
      });
    });
    if (state.page && typeof state.page.onAccountsChanged === "function") {
      state.page.onAccountsChanged();
    }
  }

  function showToast(message) {
    const toast = $("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.remove("hidden");
    window.setTimeout(() => toast.classList.add("hidden"), 3200);
  }

  function setBadge(kind, text) {
    const badge = $("taskBadge");
    if (!badge) return;
    badge.className = `status-pill ${kind}`;
    badge.textContent = text;
  }

  function isLogPinnedToBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight <= 24;
  }

  function copyTextFallback(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      return document.execCommand("copy");
    } catch (_error) {
      return false;
    } finally {
      textarea.remove();
    }
  }

  async function copyLogs() {
    const box = $("logBox");
    const logs = box ? box.textContent : "";
    if (!logs.trim()) {
      showToast("暂无日志可复制");
      return;
    }

    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(logs);
        copied = true;
      } catch (_error) {
        copied = false;
      }
    }
    if (!copied) copied = copyTextFallback(logs);
    showToast(copied ? "日志已复制" : "复制失败，请手动选中日志复制");
  }

  function initializeLogControls() {
    const copyButton = $("copyLogBtn");
    if (copyButton) copyButton.addEventListener("click", copyLogs);
  }

  function appendLog(message) {
    const box = $("logBox");
    if (!box) return;
    const pinnedToBottom = isLogPinnedToBottom(box);
    box.textContent += `${box.textContent ? "\n" : ""}${message}`;
    if (pinnedToBottom) box.scrollTop = box.scrollHeight;
  }

  function resetTaskView() {
    if ($("recordCount")) $("recordCount").textContent = "0";
    if ($("outputFile")) $("outputFile").textContent = "未生成";
    if ($("statusText")) $("statusText").textContent = "任务准备中";
    if ($("logBox")) $("logBox").textContent = "";
    state.outputFile = "";
    if (state.page && typeof state.page.resetResult === "function") state.page.resetResult();
  }

  function stopPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = 0;
  }

  function setRunning(running) {
    if (state.page && typeof state.page.setRunning === "function") state.page.setRunning(running);
  }

  function renderTask(task) {
    if (!task || !task.ok) return;
    if (typeof state.page?.taskContextFilter === "function" && !state.page.taskContextFilter(task.context || {})) return;
    renderGlobalTaskStatus(task);
    const box = $("logBox");
    if (box) {
      const pinnedToBottom = isLogPinnedToBottom(box);
      const previousScrollTop = box.scrollTop;
      box.textContent = (task.logs || []).join("\n");
      box.scrollTop = pinnedToBottom ? box.scrollHeight : previousScrollTop;
    }
    if (state.page && typeof state.page.onTaskUpdate === "function") state.page.onTaskUpdate(task);
    if (task.status === "running" || task.status === "pending") {
      setBadge("running", "运行中");
      if ($("statusText")) $("statusText").textContent = "任务运行中";
      setRunning(true);
      return;
    }
    setRunning(false);
    if (task.status === "success") {
      const businessFailed = task.result && task.result.success === false;
      const hasPartialSuccess = businessFailed && Number(task.result.success_store_count || 0) > 0;
      setBadge(businessFailed ? "failed" : "success", hasPartialSuccess ? "部分完成" : (businessFailed ? "失败" : "已完成"));
      if ($("statusText")) {
        $("statusText").textContent = businessFailed
          ? (task.result.message || (hasPartialSuccess ? "任务部分完成" : "任务失败"))
          : "任务已完成";
      }
      if (state.page && typeof state.page.applyResult === "function") state.page.applyResult(task.result || {});
    } else if (task.status === "failed") {
      if (task.result && state.page && typeof state.page.applyResult === "function") state.page.applyResult(task.result);
      const incomplete = task.result && task.result.is_complete === false;
      setBadge("failed", incomplete ? "结果不完整" : "失败");
      if ($("statusText")) $("statusText").textContent = incomplete ? (task.result.completion_message || "结果不完整，请处理失败项后继续补查") : "任务失败";
      if (task.error && box && !box.textContent.includes(task.error)) appendLog(task.error);
    }
  }

  function renderGlobalTaskStatus(task) {
    const summary = $("updateSummary");
    if (!summary || !task || !task.ok) return;
    if (task.status === "running" || task.status === "pending") {
      summary.textContent = `${task.name || "后台任务"}运行中`;
    } else if (task.status === "success") {
      summary.textContent = task.result && task.result.success === false
        ? `${task.name || "最近任务"}完成但存在失败项`
        : `${task.name || "最近任务"}已完成`;
    } else if (task.status === "failed") {
      summary.textContent = `${task.name || "最近任务"}${task.result && task.result.is_complete === false ? "结果不完整" : "执行失败"}`;
    }
  }

  async function pollTask() {
    if (!state.taskId) return;
    const requestedTaskId = state.taskId;
    try {
      const task = await api().get_task_status(requestedTaskId);
      if (state.taskId !== requestedTaskId) return;
      if (typeof state.page?.taskContextFilter === "function" && !state.page.taskContextFilter(task.context || {})) { stopPolling(); return; }
      renderTask(task);
      if (task.status === "success" || task.status === "failed") stopPolling();
    } catch (error) {
      if (state.taskId !== requestedTaskId) return;
      appendLog(error.message || String(error));
      stopPolling();
      setRunning(false);
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = window.setInterval(pollTask, 1000);
    pollTask();
  }

  async function startTask(taskFactory, isCurrentRequest = () => true) {
    resetTaskView();
    setRunning(true);
    setBadge("running", "启动中");
    try {
      const task = await taskFactory();
      if (!isCurrentRequest()) return null;
      if (!task.ok) throw new Error(task.error || "任务启动失败");
      if (typeof state.page?.taskContextFilter === "function" && !state.page.taskContextFilter(task.context || {})) return null;
      state.taskId = task.id;
      renderTask(task);
      startPolling();
      return task;
    } catch (error) {
      if (!isCurrentRequest()) return null;
      setRunning(false);
      setBadge("failed", "启动失败");
      appendLog(error.message || String(error));
      showToast(error.message || String(error));
      return null;
    }
  }

  async function restoreLatestTask(tool) {
    if (!tool || typeof api().get_latest_task_status !== "function") return;
    try {
      const task = await api().get_latest_task_status(tool);
      if (!task.ok || task.empty) return;
      state.taskId = task.id;
      renderTask(task);
      if (task.status === "running" || task.status === "pending") startPolling();
    } catch (_error) {
      // Older packaged bridges may not expose task restoration yet.
    }
  }

  async function restoreLatestTaskFromKeys(tools, contextFilter) {
    if (!Array.isArray(tools) || !tools.length || typeof api().get_latest_task_status !== "function") return null;
    const candidates = [];
    for (const tool of tools) {
      try {
        const task = await api().get_latest_task_status(tool);
        if (!task?.ok || task.empty) continue;
        if (typeof contextFilter === "function" && !contextFilter(task.context || {})) continue;
        candidates.push(task);
      } catch (_error) {
        // One unavailable tool (or an older bridge) must not prevent restoring the others.
      }
    }
    const timestamp = (task) => Date.parse(task.created_at || "") || 0;
    const live = candidates.filter((task) => task.status === "running" || task.status === "pending");
    const selected = (live.length ? live : candidates).sort((a, b) => timestamp(b) - timestamp(a))[0];
    if (!selected) return null;
    if (typeof contextFilter === "function" && !contextFilter(selected.context || {})) return null;
    state.taskId = selected.id;
    renderTask(selected);
    if (selected.status === "running" || selected.status === "pending") startPolling();
    if (state.page && typeof state.page.onTaskRestored === "function") state.page.onTaskRestored(selected);
    return selected;
  }

  async function openOutput(path = "") {
    const target = path || state.outputFile || state.outputDir;
    if (!target) return;
    const result = await api().reveal_path(target);
    if (!result.ok) showToast(result.error || "打开输出目录失败");
  }

  function ensureQuickAccountModal() {
    if ($("quickAccountModal")) return;
    document.body.insertAdjacentHTML("beforeend", `
      <div id="quickAccountModal" class="modal-backdrop hidden" role="dialog" aria-modal="true">
        <form id="quickAccountForm" class="account-dialog">
          <div class="account-dialog-header">
            <div class="account-dialog-brand"><span id="quickAccountLogo" class="account-provider-logo large">M</span><div><div id="quickAccountEyebrow" class="section-eyebrow">ACCOUNT</div><h2 id="quickAccountTitle">绑定账号</h2></div></div>
            <button id="closeQuickAccountBtn" class="modal-close" type="button">×</button>
          </div>
          <div class="account-required-notice"><strong>运行前需要绑定账号</strong><span>添加成功后会自动继续当前操作。</span></div>
          <div class="account-dialog-body">
            <label id="quickCompanyField" hidden><span>紫鸟公司名</span><input id="quickCompany" /></label>
            <label><span>账号名称 <small>选填</small></span><input id="quickAccountName" maxlength="40" /></label>
            <label><span id="quickUsernameLabel">登录账号</span><input id="quickUsername" autocomplete="username" required /></label>
            <label><span id="quickPasswordLabel">登录密码</span><input id="quickPassword" type="password" autocomplete="new-password" required /></label>
          </div>
          <div class="account-dialog-actions"><button id="cancelQuickAccountBtn" class="secondary" type="button">取消</button><button id="submitQuickAccountBtn" class="primary" type="submit">确认绑定</button></div>
        </form>
      </div>`);
    $("closeQuickAccountBtn").addEventListener("click", closeQuickAccountModal);
    $("cancelQuickAccountBtn").addEventListener("click", closeQuickAccountModal);
    $("quickAccountModal").addEventListener("click", (event) => {
      if (event.target === $("quickAccountModal")) closeQuickAccountModal();
    });
    $("quickAccountForm").addEventListener("submit", submitQuickAccount);
  }

  let quickAccountVendor = "mabang";

  function openAccountDialog(vendor, continuation = null) {
    ensureQuickAccountModal();
    quickAccountVendor = vendor;
    state.accountContinuation = continuation;
    const ziniao = vendor === "ziniao";
    const meta = vendorMeta(vendor);
    $("quickAccountForm").reset();
    $("quickCompanyField").hidden = !ziniao;
    $("quickCompany").required = ziniao;
    $("quickAccountLogo").textContent = meta.logo;
    $("quickAccountLogo").style.background = meta.color || "";
    $("quickAccountEyebrow").textContent = meta.eyebrow;
    $("quickAccountTitle").textContent = `绑定${meta.label}账号`;
    $("quickUsernameLabel").textContent = `${meta.label}账号`;
    $("quickPasswordLabel").textContent = `${meta.label}密码`;
    $("quickAccountModal").classList.remove("hidden");
    window.setTimeout(() => (ziniao ? $("quickCompany") : $("quickUsername")).focus(), 0);
  }

  function closeQuickAccountModal() {
    if ($("quickAccountModal")) $("quickAccountModal").classList.add("hidden");
    state.accountContinuation = null;
  }

  async function submitQuickAccount(event) {
    event.preventDefault();
    const button = $("submitQuickAccountBtn");
    button.disabled = true;
    button.textContent = "正在绑定...";
    try {
      const isZiniao = quickAccountVendor === "ziniao";
      const company = isZiniao ? $("quickCompany").value.trim() : "";
      const result = await api().add_account({
        vendor: quickAccountVendor,
        name: $("quickAccountName").value.trim() || company,
        username: $("quickUsername").value.trim(),
        password: $("quickPassword").value,
        extra: isZiniao ? { company } : {},
      });
      if (!result.ok) throw new Error(result.error || "账号绑定失败");
      const continuation = state.accountContinuation;
      applyAccountState(result.account_state || {});
      closeQuickAccountModal();
      showToast("账号绑定成功");
      if (typeof continuation === "function") await continuation();
    } catch (error) {
      showToast(error.message || String(error));
    } finally {
      button.disabled = false;
      button.textContent = "确认绑定";
    }
  }

  async function initialize(page) {
    state.page = page;
    renderSidebar(page.key);
    loadSidebarPreferences();
    try {
      const info = await api().get_app_info();
      if (!info.ok) throw new Error(info.error || "应用信息加载失败");
      state.info = info;
      state.outputDir = (info.settings || {}).output_dir || "";
      document.title = `${page.title} - ${info.app.name}`;
      if ($("appVersion")) $("appVersion").textContent = info.app.version;
      applyAccountState(info.account_state || {});
      if (typeof page.init === "function") await page.init(info);
      if (typeof api().get_latest_task_status === "function") {
        const latestGlobalTask = await api().get_latest_task_status();
        if (latestGlobalTask.ok) renderGlobalTaskStatus(latestGlobalTask);
      }
      if (Array.isArray(page.taskKeys) && page.taskKeys.length) {
        await restoreLatestTaskFromKeys(page.taskKeys, page.taskContextFilter);
      } else if (page.taskKey) {
        await restoreLatestTask(page.taskKey);
      }
    } catch (error) {
      showToast(error.message || String(error));
    }
  }

  function boot(page) {
    let started = false;
    const start = () => {
      if (started || !window.pywebview || !window.pywebview.api) return;
      started = true;
      initialize(page);
    };
    const domReady = () => {
      renderSidebar(page.key);
      initializeLogControls();
      const preview = location.hostname === "127.0.0.1" && new URLSearchParams(location.search).has("preview");
      if (preview && (!window.pywebview || !window.pywebview.api)) window.pywebview = { api: createPreviewApi() };
      start();
      if (!started) {
        window.addEventListener("pywebviewready", start, { once: true });
        const timer = window.setInterval(() => {
          start();
          if (started) window.clearInterval(timer);
        }, 100);
      }
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", domReady, { once: true });
    else domReady();
  }

  function createPreviewApi() {
    let accounts = [];
    let activeAccountIds = {};
    let previewVietnamManifest = null;
    let developerPermissionSnapshot = null;
    let developerPermissionSequence = 0;
    const previewParams = new URLSearchParams(location.search);
    const previewState = previewParams.get("state") || "empty";
    if (previewParams.get("account") === "echotik") {
      accounts = [{ id: "preview-echotik", vendor: "echotik", name: "EchoTik 预览账号", username: "preview@example.com", extra: {} }];
      activeAccountIds = { echotik: "preview-echotik" };
    } else if (previewParams.get("account") === "mabang") {
      accounts = [{ id: "preview-mabang", vendor: "mabang", name: "马帮预览账号", username: "13800000000", extra: {} }];
      activeAccountIds = { mabang: "preview-mabang" };
    } else if (previewParams.get("account") === "bigseller") {
      accounts = [{ id: "preview-bigseller", vendor: "bigseller", name: "BigSeller 演示账号", username: "demo@example.com", extra: {} }];
      activeAccountIds = { bigseller: "preview-bigseller" };
    } else if (previewParams.get("account") === "ziniao") {
      accounts = [{ id: "preview-ziniao", vendor: "ziniao", name: "紫鸟演示账号", username: "preview@example.com", extra: { company: "演示公司" } }];
      activeAccountIds = { ziniao: "preview-ziniao" };
    }
    const previewManifestPath = "C:\\Preview\\越南收支核对\\2026-05\\vn_202605_preview\\manifest.json";
    if (previewState !== "empty") {
      const complete = previewState === "ready_to_generate" || previewState === "final_ready";
      const validationFailed = previewState === "validation_failed";
      previewVietnamManifest = {
        schema_version: 1,
        run_id: "vn_202605_preview",
        period: "2026-05",
        status: previewState === "final_ready" ? "final_ready" : complete ? "mabang_ready" : "running",
        stores: ["越南店铺 A", "越南店铺 B"].map((store_name) => ({ store_name, reports: Object.fromEntries(["income", "ads", "affiliate", "ads_credit"].map((key) => [key, { status: complete || validationFailed ? "success" : "running" }])) })),
        mabang: {
          reports: Object.fromEntries(["income_detail", "income_summary", "refund_part_01_10", "refund_part_11_20", "refund_part_21_end", "refunds_merged"].map((key) => [key, { status: complete ? "success" : "pending", row_count: complete ? 20 : 0 }])),
          validation: { status: validationFailed ? "failed" : complete ? "passed" : "pending", order_count_diff: validationFailed ? 2 : 0, receivable_diff: validationFailed ? "158.20" : "0" },
        },
        final: previewState === "final_ready" ? { status: "ready", output_file: "output\\越南Shopee收支报表.xlsx", unmatched_count: 1, finished_at: "2026-07-11 10:20:00" } : { status: "pending" },
        config: { profit_rates: {} },
      };
    }
    const tasks = {};
    const accountState = () => ({ accounts, active_account_ids: activeAccountIds });
    const startPreviewTask = (tool, result = {}) => {
      const task = { ok: true, id: `preview-${tool}`, tool, status: "success", logs: ["预览任务已完成"], result };
      tasks[tool] = task;
      return task;
    };
    return {
      async get_app_info() { return { ok: true, app: { name: "寰球云联自动化平台", version: "0.2.66" }, date_range: { start_date: "2026-06-01", end_date: "2026-06-23" }, settings: { output_dir: "C:\\Users\\Demo\\Desktop", vietnam_last_manifest_path: previewVietnamManifest ? previewManifestPath : "", rows_per_page: 500, update_manifest_url: "", captcha_username: "preview-user", captcha_password_configured: true, dingtalk: { app_key_configured: true, app_key_masked: "din****key", app_secret_configured: true, user_count: 1, operator_names: ["王小妹"] }, sales_group_ids: ["preview-group"], income_expense_category_ids: [], income_expense_warehouse_keys: [] }, account_state: accountState(), group_sales_report: { groups: [{ id: "preview-group", name: "示例小组" }] }, mabang_income_expense_report: { date_range: { start_date: "2026-07-01", end_date: "2026-07-31" }, categories: [{ id: "1038955", name: "lazada菲律宾", mabang_name: "lazada菲律宾" }], warehouses: [{ key: "1091307", name: "实速通", warehouse_ids: ["1091307"], mabang_names: ["莫斯科五号仓-实速通"], note: "" }, { key: "1100204", name: "印尼雅仓海外仓", warehouse_ids: ["1100204"], mabang_names: ["ID8803-雅仓海外仓"], note: "" }, { key: "1100500", name: "菲律宾雅仓海外仓", warehouse_ids: ["1100500"], mabang_names: ["PH8807-雅仓海外仓"], note: "" }, { key: "1096191", name: "马来雅仓海外仓", warehouse_ids: ["1096191"], mabang_names: ["MY8805-雅仓海外仓"], note: "" }] } }; },
      async get_latest_task_status(tool) { return tasks[tool] || { ok: false, empty: true }; },
      async get_task_status(id) { return Object.values(tasks).find((task) => task.id === id) || { ok: false, error: "任务不存在" }; },
      async get_group_sales_options() { return { ok: true, groups: [{ id: "preview-group", name: "菲S-示例小组" }, { id: "preview-tk", name: "tk菲律宾" }, { id: "preview-lazada", name: "lazada马来" }] }; },
      async start_group_sales_report(payload) { return startPreviewTask("group_sales", { group_count: (payload.group_ids || []).length, record_count: 16, output_file: "C:\\Preview\\销量.xlsx", output_dir: "C:\\Preview" }); },
      async start_developer_sales_income_summary() { return startPreviewTask("developer_sales_income_summary", { developer_count: 2, salesperson_count: 4, source_row_count: 86, missing_developer_names: [], output_file: "C:\\Preview\\开发与销售收入汇总.xlsx", output_dir: "C:\\Preview" }); },
      async start_mabang_income_expense_report() { return startPreviewTask("mabang_income_expense_report", { target_count: 3, category_count: 0, warehouse_count: 3, csv_file_count: 3, record_count: 1286, output_files: ["C:\\Preview\\印尼雅仓海外仓_收支报表.csv", "C:\\Preview\\菲律宾雅仓海外仓_收支报表.csv", "C:\\Preview\\马来雅仓海外仓_收支报表.csv"], output_dir: "C:\\Preview\\马帮收支报表" }); },
      async start_purchase_log_query() { return startPreviewTask("purchase_log", { sku_count: 2, record_count: 8, output_file: "C:\\Preview\\采购.xlsx", output_dir: "C:\\Preview" }); },
      async start_mabang_arrival_query() { return startPreviewTask("mabang_arrival_query", { remark_count: 3, matched_remark_count: 2, matched_batch_count: 2, exported_batch_count: 2, unmatched_remarks: ["未命中示例备注"], output_file: "C:\\Preview\\到货查询\\到货查询_20260825_090000_第一车.xls", output_files: ["C:\\Preview\\到货查询\\到货查询_20260825_090000_第一车.xls", "C:\\Preview\\到货查询\\到货查询_20260825_090000_第二车.xls"], file_count: 2, output_dir: "C:\\Preview\\到货查询", output_root: "C:\\Preview", exports: [{ remark: "第一车", matched_batch_count: 1, output_file: "C:\\Preview\\到货查询\\到货查询_20260825_090000_第一车.xls" }, { remark: "第二车", matched_batch_count: 1, output_file: "C:\\Preview\\到货查询\\到货查询_20260825_090000_第二车.xls" }] }); },
      async get_sku_inventory_developers() { return { ok: true, developer_count: 3, developers: [{ id: "101", name: "张三-开发" }, { id: "102", name: "李四-欧美开发" }, { id: "103", name: "王五-开发" }] }; },
      async start_sku_inventory_query(payload = {}) {
        if (payload.query_mode === "missing_warehouses") {
          const selected = new Set(payload.liveness_types || ["1", "2"]);
          const scope = [{ id: "TH01", name: "泰国TH01仓", country: "泰国" }, { id: "PH01", name: "菲律宾PH01仓", country: "菲律宾" }, { id: "MY01", name: "马来西亚MY01仓", country: "马来西亚" }];
          const rows = (payload.developer_ids || ["101"]).flatMap((id) => [
            { developer_name: ({ "101": "张三-开发", "102": "李四-欧美开发", "103": "王五-开发" })[id] || "示例开发员", sku: `DEMO-${id}-001`, product_name: "示例爆款商品", liveness_type: "1", liveness_name: "爆款", missing_warehouse_count: 2, covered_warehouse_count: 1, missing_warehouse_names: "菲律宾PH01仓、马来西亚MY01仓" },
            { developer_name: ({ "101": "张三-开发", "102": "李四-欧美开发", "103": "王五-开发" })[id] || "示例开发员", sku: `DEMO-${id}-002`, product_name: "示例旺款商品", liveness_type: "2", liveness_name: "旺款", missing_warehouse_count: 0, covered_warehouse_count: 3, missing_warehouse_names: "" },
          ]).filter((row) => selected.has(row.liveness_type));
          return startPreviewTask("sku_inventory_query", { query_mode: "missing_warehouses", developer_count: (payload.developer_ids || ["101"]).length, sku_count: rows.length, record_count: rows.length, warehouse_count: scope.length, scope_warehouses: scope, missing_sku_count: rows.filter((row) => row.missing_warehouse_count > 0).length, output_file: "C:/Preview/爆款旺款缺失仓库.xlsx", output_dir: "C:/Preview", preview_limit: 500, preview_truncated: false, rows });
        }
        return startPreviewTask("sku_inventory_query", { developer_count: 2, sku_count: 2, warehouse_count: 3, record_count: 3, output_file: "C:\\Preview\\SKU库存与可售天数.xlsx", output_dir: "C:\\Preview", preview_limit: 500, preview_truncated: false, rows: [{ developer_name: "张三-开发", sku: "SKU-001", product_name: "示例商品一", created_at: "2026-08-01 10:30:00", warehouse_name: "菲律宾PH01仓", warehouse_id: "1080816", available_inventory: 120, forecast_daily_sales: 6, current_sales_days: 20, unshipped_count: 0, transit_inventory: 0 }, { developer_name: "张三-开发", sku: "SKU-001", product_name: "示例商品一", created_at: "2026-08-01 10:30:00", warehouse_name: "泰国新仓TH01", warehouse_id: "330978", available_inventory: 0, forecast_daily_sales: 3, current_sales_days: 0, unshipped_count: 7, transit_inventory: 0 }, { developer_name: "李四-欧美开发", sku: "SKU-002", product_name: "示例商品二", created_at: "2026-08-08", warehouse_name: "马来西亚MY01仓", warehouse_id: "1070924", available_inventory: 0, forecast_daily_sales: "--", current_sales_days: 0, unshipped_count: 0, transit_inventory: 100 }] }); },
      async start_mabang_developer_permission_preview(payload) {
        const names = Array.isArray(payload.employee_names) ? [...payload.employee_names] : [];
        const rows = names.map((name, index) => {
          const status = ["ready", "unchanged", "not_found", "ambiguous"][index % 4];
          const matched = status === "ready" || status === "unchanged";
          return {
            input_name: name, employee_id: matched ? String(1117519 + index) : "", employee_name: matched ? name : "",
            mobile: matched ? `1380000${String(index).padStart(4, "0")}` : "", department: matched ? "演示运营部" : "",
            status, can_apply: status === "ready", has_developer: status === "unchanged",
            current_station_names: matched ? (status === "unchanged" ? ["销售员", "开发员"] : ["销售员"]) : [],
            product_view_mode: matched ? (status === "unchanged" ? "1" : "2") : "",
            message: `[演示数据] ${{ ready: "将添加开发员并设置按商品父目录查看", unchanged: "已有开发员岗位，且已按商品父目录查看", not_found: "未找到姓名完全匹配的员工", ambiguous: "存在同名员工，请核实后再处理" }[status]}`,
          };
        });
        const token = `preview-developer-${++developerPermissionSequence}`;
        developerPermissionSnapshot = { account_id: payload.account_id, employee_names: names, token, rows };
        const task = startPreviewTask("mabang_developer_permission_preview", {
          mode: "preview", preview_token: token, employee_count: rows.length,
          ready_count: rows.filter((row) => row.can_apply).length,
          skipped_count: rows.filter((row) => row.status === "unchanged").length,
          attention_count: rows.filter((row) => ["not_found", "ambiguous"].includes(row.status)).length, rows,
        });
        task.context = { account_id: payload.account_id, employee_names: names, mode: "preview" };
        task.created_at = new Date().toISOString();
        task.logs = ["[演示数据] 使用本地界面预览机制，未连接马帮。", ...rows.map((row) => `${row.input_name}：${row.message}`)];
        return task;
      },
      async start_mabang_developer_permission_batch(payload) {
        const snapshot = developerPermissionSnapshot;
        if (!snapshot || payload.preview_token !== snapshot.token || payload.account_id !== snapshot.account_id || JSON.stringify(payload.employee_names) !== JSON.stringify(snapshot.employee_names)) {
          return { ok: false, error: "演示预览已失效，请重新预览检查" };
        }
        developerPermissionSnapshot = null;
        const rows = snapshot.rows.filter((row) => row.can_apply).map((row) => ({
          ...row, status: "success", can_apply: false, has_developer: true,
          current_station_names: [...new Set([...row.current_station_names, "开发员"])], product_view_mode: "1",
          message: "[演示数据] 已模拟添加开发员并保存按商品父目录查看；原岗位、目录、仓库和其他权限保留",
        }));
        const task = startPreviewTask("mabang_developer_permission_batch", {
          mode: "batch", employee_count: rows.length, success_count: rows.filter((row) => row.status === "success").length,
          skipped_count: rows.filter((row) => row.status === "skipped").length, failed_count: 0, rows,
        });
        task.context = { account_id: payload.account_id, employee_names: [...snapshot.employee_names], mode: "batch" };
        task.created_at = new Date().toISOString();
        task.logs = ["[演示数据] 仅模拟批量保存，未修改马帮员工。", ...rows.map((row) => `${row.input_name}：${row.message}`)];
        return task;
      },
      async get_mabang_warehouse_options() { return { ok: true, warehouse_count: 8, warehouses: [{ id: "169813", name: "荆州海外仓中转仓" }, { id: "239944", name: "武汉仓" }, { id: "314510", name: "广州海外仓中转仓" }, { id: "330978", name: "泰国新仓TH01" }, { id: "390451", name: "菲律宾PH02中转仓" }, { id: "410001", name: "马来西亚MY01仓" }, { id: "410002", name: "越南河内仓-kerry" }, { id: "410003", name: "测试仓库" }] }; },
      async start_mabang_warehouse_permission_preview(payload) {
        const names = Array.isArray(payload.employee_names) ? payload.employee_names : [];
        const selected = Array.isArray(payload.warehouse_ids) ? payload.warehouse_ids : [];
        const permissionTypes = Array.isArray(payload.permission_types) ? payload.permission_types : ["product", "order", "warehouse"];
        const missingNames = selected.map((id) => `仓库 ${id}`);
        const rows = names.map((name, index) => index === 1 ? {
          input_name: name, employee_id: "", employee_name: "", mobile: "", department: "",
          status: "not_found", message: "未找到姓名完全匹配的员工", can_apply: false,
          missing_product_warehouse_ids: [], missing_product_warehouse_names: [],
          missing_order_warehouse_ids: [], missing_order_warehouse_names: [],
          missing_view_warehouse_ids: [], missing_view_warehouse_names: [], target_warehouse_count: selected.length,
        } : {
          input_name: name, employee_id: String(1117519 + index), employee_name: name,
          mobile: `1380000000${index}`, department: "运营部", status: "ready",
          message: permissionTypes.map((type) => `${{ product: "查看商品", order: "查看订单", warehouse: "查看仓库" }[type]}缺 ${selected.length} 个`).join("；"), can_apply: true,
          missing_product_warehouse_ids: permissionTypes.includes("product") ? selected : [], missing_product_warehouse_names: permissionTypes.includes("product") ? missingNames : [],
          missing_order_warehouse_ids: permissionTypes.includes("order") ? selected : [], missing_order_warehouse_names: permissionTypes.includes("order") ? missingNames : [],
          missing_view_warehouse_ids: permissionTypes.includes("warehouse") ? selected : [], missing_view_warehouse_names: permissionTypes.includes("warehouse") ? missingNames : [],
          target_warehouse_count: selected.length,
        });
        return startPreviewTask("mabang_warehouse_permission_preview", { mode: "preview", preview_token: "preview-token", employee_count: rows.length, ready_count: rows.filter((row) => row.can_apply).length, skipped_count: 0, attention_count: rows.filter((row) => !row.can_apply).length, permission_types: permissionTypes, rows });
      },
      async start_mabang_warehouse_permission_batch(payload) { const names = Array.isArray(payload.employee_names) ? payload.employee_names : []; const added = Array.isArray(payload.warehouse_ids) ? payload.warehouse_ids.length : 0; const permissionTypes = Array.isArray(payload.permission_types) ? payload.permission_types : ["product", "order", "warehouse"]; const labels = { product: "查看商品", order: "查看订单", warehouse: "查看仓库" }; const rows = names.slice(0, Math.max(1, names.length - 1)).map((name, index) => ({ input_name: name, employee_id: String(1117519 + index), employee_name: name, mobile: `1380000000${index}`, department: "运营部", status: "success", added_count: added, product_added_count: permissionTypes.includes("product") ? added : 0, order_added_count: permissionTypes.includes("order") ? added : 0, warehouse_added_count: permissionTypes.includes("warehouse") ? added : 0, message: permissionTypes.map((type) => `${labels[type]}补齐 ${added} 个`).join("；") })); return startPreviewTask("mabang_warehouse_permission_batch", { mode: "batch", employee_count: rows.length, success_count: rows.length, skipped_count: 0, failed_count: 0, permission_types: permissionTypes, rows }); },
      async start_shopee_ads_recharge() { return startPreviewTask("shopee_ads", { matched_store_count: 2, processed_store_count: 2, output_dir: "C:\\Preview" }); },
      async get_lazada_monthly_report_info() { return { ok: true, countries: [{ code: "TH", name: "泰国" }, { code: "MY", name: "马来西亚" }, { code: "PH", name: "菲律宾" }], default_month: "2026-07", client_path: "C:\\Program Files\\Ziniao\\ziniao.exe", webdriver_path: "C:\\ziniaodriver" }; },
      async start_lazada_monthly_report(payload) {
        const countryNames = { TH: "泰国", MY: "马来西亚", PH: "菲律宾" };
        const stores = Array.from(new Set(String(payload.store_names || "").split(/\r?\n/).map((name) => name.trim()).filter(Boolean)));
        const outputRoot = payload.output_dir || "C:\\Preview";
        const outputDir = `${outputRoot}\\Lazada月度账单\\${payload.month || "2026-07"}`;
        return startPreviewTask("lazada_monthly_report", {
          country: payload.country,
          month: payload.month,
          input_store_count: stores.length,
          matched_store_count: stores.length,
          processed_store_count: stores.length,
          success_store_count: stores.length,
          failed_store_count: 0,
          partial_store_count: 0,
          output_dir: outputDir,
          stores: stores.map((storeName) => ({
            country: payload.country,
            country_name: countryNames[payload.country] || payload.country,
            store_name: storeName,
            month: payload.month,
            status: "success",
            output_file: `${outputDir}\\${storeName}.xlsx`,
            message: "月度账单已下载并按店铺命名",
          })),
        });
      },
      async get_lazada_ads_data_info() {
        const now = new Date();
        const pad = (value) => String(value).padStart(2, "0");
        let preferences = {};
        try { preferences = JSON.parse(window.localStorage.getItem("hqyl.preview.lazadaAdsPreferences") || "{}") || {}; } catch (_error) { /* Preview defaults remain available. */ }
        return {
          ok: true,
          default_sheet_name: `${String(now.getFullYear()).slice(-2)}年${now.getMonth() + 1}月`,
          default_target_date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
          saved_preferences: { client_path: preferences.client_path || "", workbook_id: preferences.workbook_id || "" },
          workbook_id: preferences.workbook_id || "preview-lazada-workbook", operator_names: ["王小妹"], dingtalk_configured: true,
          client_path: preferences.client_path || "C:\\Program Files\\Ziniao\\ziniao.exe", webdriver_path: "C:\\ziniaodriver", output_dir: "C:\\Preview",
        };
      },
      async save_lazada_ads_preferences(payload) {
        try {
          const stored = JSON.parse(window.localStorage.getItem("hqyl.preview.lazadaAdsPreferences") || "{}") || {};
          const preferences = Object.fromEntries(["client_path", "workbook_id"].filter((key) => typeof stored[key] === "string").map((key) => [key, stored[key]]));
          for (const key of ["client_path", "workbook_id"]) {
            if (!Object.prototype.hasOwnProperty.call(payload, key)) continue;
            let value = String(payload[key] || "").trim();
            if (key === "client_path") value = value.replace(/^"+|"+$/g, "");
            if (key === "workbook_id" && value) {
              if (/^https?:\/\//i.test(value)) {
                const url = new URL(value);
                const query = new Map(Array.from(url.searchParams, ([name, item]) => [name.toLowerCase(), item]));
                const path = url.pathname.match(/\/(?:spreadsheetv2|i\/nodes)\/([^/?#]+)/i);
                value = query.get("dockey") || (path && path[1]) || query.get("dentrykey") || "";
              }
              if (!/^[A-Za-z0-9_-]+$/.test(value)) return { ok: false, error: "钉钉工作簿 ID 格式无效" };
            }
            if (value) preferences[key] = value;
            else delete preferences[key];
          }
          window.localStorage.setItem("hqyl.preview.lazadaAdsPreferences", JSON.stringify(preferences));
          return { ok: true };
        } catch (error) {
          return { ok: false, error: error.message || "演示配置保存失败" };
        }
      },
      async start_lazada_ads_data(payload) {
        const now = new Date();
        const pad = (value) => String(value).padStart(2, "0");
        const sheetName = String(payload.sheet_name || "").trim() || `${String(now.getFullYear()).slice(-2)}年${now.getMonth() + 1}月`;
        const targetDate = String(payload.target_date || "").trim() || `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
        const inputStores = Array.isArray(payload.store_names) ? payload.store_names : String(payload.store_names || "").split(/\r?\n/);
        const stores = Array.from(new Set(inputStores.map((name) => name.trim()).filter(Boolean)));
        const task = startPreviewTask("lazada_ads_data", {
          is_preview: true, success: true, message: "演示数据：未连接 Lazada，未写入钉钉，未生成本地结果。",
          sheet_name: sheetName, target_date: targetDate,
          input_store_count: stores.length, matched_store_count: stores.length, success_store_count: stores.length,
          skipped_store_count: 0, failed_store_count: 0, output_dir: "", output_file: "",
          stores: stores.map((storeName, index) => ({
            store_name: storeName, target_date: targetDate, status: "success",
            advertising: (100 + index * 25.5).toFixed(2), performance: (1200 + index * 380.5).toFixed(2),
            message: `演示结果；目标 Sheet：${sheetName}，未实际写入`, written_cells: [],
          })),
        });
        task.logs = ["演示模式：未登录 Lazada，未读取真实广告数据，未写入钉钉。", `已展示 ${stores.length} 个店铺的演示结果。`];
        return task;
      },
      async get_lazada_bill_detail_info() {
        const now = new Date();
        const pad = (value) => String(value).padStart(2, "0");
        let preferences = {};
        try { preferences = JSON.parse(window.localStorage.getItem("hqyl.preview.lazadaBillPreferences") || "{}") || {}; } catch (_error) { /* Preview defaults remain available. */ }
        const previousMonthEnd = new Date(now.getFullYear(), now.getMonth(), 0);
        const previousMonthStart = new Date(previousMonthEnd.getFullYear(), previousMonthEnd.getMonth(), 1);
        const iso = (value) => `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
        return {
          ok: true,
          countries: [
            { code: "TH", name: "泰国", currency: "(?:฿|THB)", default_workbook_id: "14lgGw3P8vvv7b2gCgg3PkQr85daZ90D", default_dws_node: "" },
            { code: "PH", name: "菲律宾", currency: "(?:₱|PHP)", default_workbook_id: "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", default_dws_node: "" },
            { code: "MY", name: "马来西亚", currency: "(?:RM|MYR)", default_workbook_id: "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", default_dws_node: "" },
            { code: "ID", name: "印尼", currency: "(?:Rp|IDR)", default_workbook_id: "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", default_dws_node: "" },
            { code: "VN", name: "越南", currency: "(?:₫|VND)", default_workbook_id: "pLdn55X2E5o4yno8", default_dws_node: "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l" },
          ],
          default_sheet_name: `${String(now.getFullYear()).slice(-2)}年${now.getMonth() + 1}月`,
          date_range: { start_date: iso(previousMonthStart), end_date: iso(previousMonthEnd) },
          workbook_id: preferences.workbook_id || "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
          dws_node: "", image_column: "F",
          output_dir: "C:\\Preview", screenshot_root: preferences.screenshot_root || "C:\\Users\\Demo\\Desktop\\log",
          screenshot_dir: preferences.screenshot_root || "C:\\Users\\Demo\\Desktop\\log",
          // 预览模式：加 ?dws=0 可以演示「未安装 dws」的告警状态。
          ...(() => {
            const previewDws = new URLSearchParams(location.search).get("dws") !== "0";
            return {
              dws_available: previewDws,
              dws_path: previewDws ? "C:\\Users\\Demo\\.local\\bin\\dws.exe" : "",
              dws_hint: previewDws ? "" : "本机未安装 dws（PATH 中没有 dws 命令）：本次不会写入钉钉 F 列图片，截图仍会保存到「截图保存位置」。请把它所在目录加入系统 PATH 后重开应用。",
            };
          })(),
          operator_names: ["王小妹"], default_dingtalk_operator_name: "王小妹", dingtalk_configured: true,
          client_path: preferences.client_path || "C:\\Program Files\\Ziniao\\ziniao.exe", webdriver_path: "C:\\ziniaodriver",
        };
      },
      async list_lazada_bill_sheets(payload) {
        if (!payload || !payload.country) return { ok: false, error: "请选择国家" };
        const now = new Date();
        const countryNames = { TH: "泰国", PH: "菲律宾", MY: "马来", ID: "印尼", VN: "越南" };
        const currentMonthSheet = `${String(now.getFullYear()).slice(-2)}年${now.getMonth() + 1}月`;
        const sheetNames = [...Object.values(countryNames), currentMonthSheet];
        return {
          ok: true, workbook_id: payload.workbook_id || "preview-workbook",
          country: payload.country, country_name: countryNames[payload.country] || payload.country,
          default_sheet_name: currentMonthSheet, preferred_sheet_name: countryNames[payload.country] || "",
          sheet_names: sheetNames, sheets: sheetNames.map((name, index) => ({ name, id: `preview-sheet-${index + 1}` })),
        };
      },
      async save_lazada_bill_preferences(payload) {
        try {
          const stored = JSON.parse(window.localStorage.getItem("hqyl.preview.lazadaBillPreferences") || "{}") || {};
          const preferences = { client_path: stored.client_path || "", screenshot_root: stored.screenshot_root || "", workbook_id: stored.workbook_id || "", dws_node: stored.dws_node || "" };
          for (const key of ["client_path", "screenshot_root", "workbook_id", "dws_node"]) {
            if (Object.prototype.hasOwnProperty.call(payload, key)) preferences[key] = String(payload[key] || "").trim();
          }
          window.localStorage.setItem("hqyl.preview.lazadaBillPreferences", JSON.stringify(preferences));
          return { ok: true };
        } catch (error) {
          return { ok: false, error: error.message || "演示配置保存失败" };
        }
      },
      async start_lazada_bill_detail(payload) {
        const now = new Date();
        const sheetName = String(payload.sheet_name || "").trim() || `${String(now.getFullYear()).slice(-2)}年${now.getMonth() + 1}月`;
        const inputStores = Array.isArray(payload.store_names) ? payload.store_names : String(payload.store_names || "").split(/\r?\n/);
        const stores = Array.from(new Set(inputStores.map((name) => name.trim()).filter(Boolean)));
        const task = startPreviewTask("lazada_bill_detail", {
          is_preview: true, success: true, message: "演示数据：未连接 Lazada，未写入钉钉，未生成本地截图。",
          country: payload.country, country_name: { TH: "泰国", PH: "菲律宾", MY: "马来西亚", ID: "印尼", VN: "越南" }[payload.country] || payload.country,
          sheet_name: sheetName, start_date: payload.start_date, end_date: payload.end_date,
          workbook_id: payload.workbook_id || "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
          screenshot_root: payload.screenshot_root || "C:\\Users\\Demo\\Desktop\\log",
          screenshot_dir: `${payload.screenshot_root || "C:\\Users\\Demo\\Desktop\\log"}\\Lazada${{ TH: "泰国", PH: "菲律宾", MY: "马来西亚", ID: "印尼", VN: "越南" }[payload.country] || payload.country}账单明细截图\\${payload.start_date}到${payload.end_date}`,
          image_sync_note: "演示模式：未上传钉钉 F 列图片，截图仅本地保存",
          input_store_count: stores.length, matched_store_count: stores.length, success_store_count: stores.length,
          skipped_store_count: 0, failed_store_count: 0, image_uploaded_count: 0, image_failed_count: 0, image_local_only_count: stores.length,
          output_dir: payload.output_root || "C:\\Preview", output_file: "",
          stores: stores.map((storeName, index) => ({
            store_name: storeName, country: payload.country, country_name: { TH: "泰国", PH: "菲律宾", MY: "马来西亚", ID: "印尼", VN: "越南" }[payload.country] || payload.country, sheet_name: sheetName,
            start_date: payload.start_date, end_date: payload.end_date, row: index + 2, status: "success",
            total_amount: (5000 + index * 1250.5).toFixed(2), revenue: (6800 + index * 980.5).toFixed(2), deductions: (120 + index * 40).toFixed(2),
            message: `演示结果；目标 Sheet：${sheetName}，未实际写入`, written_range: "", screenshot: "",
            image_status: "local_only", image_cell: `F${index + 2}`,
          })),
        });
        task.logs = ["演示模式：未登录 Lazada，未读取真实账单，未写入钉钉。", `已展示 ${stores.length} 个店铺的演示结果。`];
        return task;
      },
      async start_bigseller_sync() { return startPreviewTask("bigseller_sync", { processed_count: 300, success_count: 300, failed_count: 0, round_count: 1, output_file: "C:\\Preview\\BigSeller同步.xlsx", output_dir: "C:\\Preview" }); },
      async start_bigseller_item_id_query(payload) {
        const skus = Array.from(new Set(String(payload.sku_text || "").split(/[\r\n,，;；\t]+/).map((sku) => sku.trim()).filter(Boolean)));
        const rows = skus.map((sku, index) => ({ sku, item_id: index % 3 === 1 ? "" : `DEMO-ITEM-${String(index + 1).padStart(4, "0")}`, status: index % 3 === 1 ? "not_found" : "matched", message: "演示数据；未连接 BigSeller" }));
        const matched = rows.filter((row) => row.item_id).length;
        const task = startPreviewTask("bigseller_item_id_query", { is_preview: true, sku_count: rows.length, matched_count: matched, not_found_count: rows.length - matched, failed_count: 0, rows: rows.slice(0, 500), preview_limited: rows.length > 500, output_file: "", output_dir: "" });
        task.logs = ["演示模式：未登录 BigSeller，未进行真实查询，未生成 Excel。", `已展示 ${rows.length} 个 SKU 的演示结果。`];
        return task;
      },
      async choose_bigseller_benchmark_file() { return { ok: true, path: "C:\\Preview\\滞销SKU示例.xlsx", sheets: ["Sheet1", "滞销SKU"], sheet_name: "Sheet1" }; },
      async choose_bigseller_claim_file() { return { ok: true, path: "C:\\Preview\\新品认领示例.xlsx", sheets: ["新sku上架"], sheet_name: "新sku上架", warnings: [] }; },
      async inspect_bigseller_claim_file(sourceFile) { return sourceFile ? { ok: true, path: sourceFile, sheets: ["新sku上架"], sheet_name: "新sku上架", warnings: [] } : { ok: false, error: "请选择来源 Excel" }; },
      async choose_bigseller_claim_checkpoint() { return { ok: false, error: "演示模式不读取进度文件，请在桌面平台中恢复查询" }; },
      async start_bigseller_claim_query(payload) {
        if (payload.resume_checkpoint) return { ok: false, error: "演示模式不支持恢复查询" };
        const rows = [
          { sku: "DEMO-SKU-001", shop_name: "演示店铺 A", item_id: "DEMO-ITEM-001", created_time: "2026-01-03 09:00:00", listed_time: "2026-01-05 10:00:00", status: "matched", message: "演示数据：两个时间均来自创建最早的同一商品" },
          { sku: "DEMO-SKU-002", shop_name: "演示店铺 B", item_id: "DEMO-ITEM-002", created_time: "2026-02-06 11:00:00", listed_time: "", status: "matched", message: "演示数据：平台创建时间缺失，上架时间留空" },
          { sku: "DEMO-SKU-003", shop_name: "", item_id: "", created_time: "", listed_time: "", status: "not_found", message: "演示数据：所选站点在售商品未匹配主 SKU" },
          { sku: "DEMO-SKU-004", shop_name: "", item_id: "", created_time: "", listed_time: "", status: "failed", message: "演示数据：查询失败，可以在实际任务中续查" },
        ];
        const task = startPreviewTask("bigseller_claim_query", {
          is_preview: true, is_complete: false, completion_message: "演示结果包含 1 个失败项，未连接 BigSeller",
          source_file: payload.source_file, sheet_name: payload.sheet_name, site: payload.site || "all", listing_scope: "live",
          total_rows: 5, sku_count: 4, matched_count: 2, not_found_count: 1, failed_count: 1, confirmed_count: 3,
          rows, preview_limited: false, output_file: "", output_dir: "", checkpoint_file: "",
        });
        task.status = "failed";
        task.error = task.result.completion_message;
        task.context = { ...payload, sku_count: 4 };
        task.logs = ["演示模式未读取实际 Excel、未登录 BigSeller，也未生成文件。", "[BS认领进度 3/4] 演示结果已展示"];
        return task;
      },
      async choose_bigseller_benchmark_checkpoint() { return { ok: false, error: "演示模式不读取真实进度文件；请在桌面平台中恢复查询" }; },
      async inspect_bigseller_benchmark_file(sourceFile) { return sourceFile ? { ok: true, sheets: ["Sheet1", "滞销SKU"], sheet_name: "Sheet1" } : { ok: false, error: "请选择来源 Excel" }; },
      async start_bigseller_sku_benchmark(payload) {
        if (payload.resume_checkpoint) return { ok: false, error: "演示模式不支持恢复查询；请在桌面平台中使用真实进度文件" };
        const metric = payload.metric === "sales" ? "sales" : "views";
        const rows = [
          { sku: "DEMO-SKU-001", shop_name: "演示店铺 A", metric_value: metric === "sales" ? 128 : 12600, shop_count: 3, item_id: "DEMO-ITEM-001", status: "matched", message: "演示数据：每店取最高单商品指标后比较" },
          { sku: "DEMO-SKU-002", shop_name: "演示店铺 B", metric_value: 0, shop_count: 1, item_id: "DEMO-ITEM-002", status: "matched", message: "演示数据：指标为 0，仍属于已匹配商品" },
          { sku: "DEMO-SKU-003", shop_name: "", metric_value: null, shop_count: 0, item_id: "", status: "not_found", message: "演示数据：在售商品中没有匹配 SKU" },
          { sku: "DEMO-SKU-004", shop_name: "", metric_value: null, shop_count: 0, item_id: "", status: "failed", message: "演示数据：分页请求异常，不能视为没有商品" },
        ];
        const task = startPreviewTask("bigseller_sku_benchmark", { is_preview: true, is_complete: false, completion_message: "结果不完整：演示 1 个查询失败项；演示模式不支持补查", confirmed_count: 3, retry_rounds_used: 3, checkpoint_file: "", failed_skus: ["DEMO-SKU-004"], recovered_count: 0, resumed_count: 0, metric, metric_label: metric === "sales" ? "销量" : "浏览量", source_file: payload.source_file, sheet_name: payload.sheet_name, total_rows: 5, sku_count: rows.length, matched_count: 2, not_found_count: 1, failed_count: 1, rows, preview_limited: false, output_file: "", output_dir: "" });
        task.status = "failed";
        task.error = task.result.completion_message;
        task.context = { ...payload, sku_count: rows.length };
        task.logs = ["演示模式：未读取实际 Excel，未登录 BigSeller，未生成文件。", "[BS对标进度 3/4] 已展示匹配、零指标、未匹配与查询失败四类示例；失败项未计入已确认进度。"];
        return task;
      },
      async start_echotik_collection() { return startPreviewTask("echotik_collect", { product_candidate_count: 58, product_count: 21, creator_source_count: 1680, creator_count: 1280, creator_filtered_count: 400, creator_missing_metric_count: 12, failed_product_count: 0, output_file: "C:\\Preview\\EchoTik商品达人采集.xlsx", output_dir: "C:\\Preview", products_preview: [{ product_id: "1734400948172129805", product_title: "OUKEYA HOT KISS LIP MATTE", shop_name: "Oukeya Thailand Shop", creator_count: "2661" }], creators_preview: [{ creator_id: "pimrypie__tiktok", creator_name: "Pimrypie", creator_categories: "口红与唇彩 / 散粉", creator_sales: "20.35万", fans_count: "1657.05万", video_play_count: "14.76亿", product_gmv: "฿6810.53", video_count: "18" }] }); },
      async get_echotik_filter_options() { return { ok: true, categories: [{ id: "beauty", name: "美妆个护", children: [{ id: "beauty-lips", name: "唇部彩妆", children: [] }, { id: "beauty-face", name: "面部彩妆", children: [] }, { id: "beauty-skincare", name: "面部护理", children: [] }] }, { id: "home", name: "家居、家具和电器", children: [{ id: "home-kitchen", name: "厨房用品", children: [] }, { id: "home-storage", name: "家居收纳", children: [] }] }, { id: "fashion", name: "女装与女士内衣", children: [{ id: "fashion-dresses", name: "连衣裙", children: [] }, { id: "fashion-tops", name: "女士上衣", children: [] }] }, { id: "food", name: "食品饮料", children: [] }, { id: "health", name: "保健", children: [] }, { id: "sports", name: "运动与户外", children: [] }], presets: {}, cached: true }; },
      async add_account(payload) { const account = { id: `preview-${Date.now()}`, vendor: payload.vendor, name: payload.name || payload.username, username: payload.username, extra: payload.extra || {} }; accounts = [...accounts, account]; activeAccountIds = { ...activeAccountIds, [payload.vendor]: account.id }; return { ok: true, account_state: accountState() }; },
      async select_account(vendor, id) { activeAccountIds = { ...activeAccountIds, [vendor]: id }; return { ok: true, account_state: accountState() }; },
      async delete_account(id) { const deleted = accounts.find((item) => item.id === id); accounts = accounts.filter((item) => item.id !== id); if (deleted) delete activeAccountIds[deleted.vendor]; return { ok: true, account_state: accountState() }; },
      async save_settings(payload) { return { ok: true, settings: payload }; },
      async query_captcha_balance() { return { ok: true, data: { balance: "88.50" } }; },
      async choose_output_dir() { return { ok: true, path: "C:\\Users\\Demo\\Desktop" }; },
      async get_kec_reconciliation_info() { return { ok: true, settings: {}, account_state: accountState(), basis: "实际体积（长 × 宽 × 高 × 数量 × 0.000001）" }; },
      async inspect_kec_reconciliation_source(payload) {
        if (!payload || !payload.input_file) return { ok: false, error: "请选择 KEC 账单 Excel" };
        return {
          ok: true,
          input_file: payload.input_file,
          sheet_titles: { storage: "仓储费", unloading: "卸货费", outbound: "出库+耗材", "return": "退件上架费", misc: "杂费" },
          columns: { "长(CM)": "长(CM)", "宽(CM)": "宽(CM)", "高(CM)": "高(CM)", "数量(PCS)": "数量(PCS)", "Total CBM": "Total CBM within period" },
          warnings: [],
        };
      },
      async choose_kec_reconciliation_file(payload) {
        if ((payload || {}).kind === "quote") return { ok: true, kind: "quote", path: "C:\\Preview\\KEC报价表.xlsx", name: "KEC报价表.xlsx" };
        return {
          ok: true, kind: "input", path: "C:\\Preview\\KEC对账账单.xlsx", name: "KEC对账账单.xlsx",
          input_file: "C:\\Preview\\KEC对账账单.xlsx",
          sheet_titles: { storage: "仓储费", unloading: "卸货费", outbound: "出库+耗材", "return": "退件上架费", misc: "杂费" },
          columns: {}, warnings: [],
        };
      },
      async start_kec_reconciliation(payload) {
        const summary = {
          "仓储费_账单金额": 21603.09, "仓储费_核对金额": 19694.02, "仓储费_异常数": 0,
          "仓储费_体积差额合计": 1487.521055, "仓储费_缺失尺寸行数": 0,
          "卸货费_账单金额": 1836.0, "卸货费_核对金额": 1836.0, "卸货费_异常数": 0,
          "出库+耗材_账单金额": 9210.5, "出库+耗材_核对金额": 9180.5, "出库+耗材_异常数": 2,
          "出库+耗材_未命中订单数": 2, "出库+耗材_数量不一致数": 0,
          "退件上架费_账单金额": 486.0, "退件上架费_核对金额": 486.0, "退件上架费_异常数": 0, "退件上架费_待确认数": 1,
          "杂费_账单金额": 320.0, "杂费_核对金额": 300.0, "杂费_异常数": 0, "杂费_待确认数": 3,
          "出库订单数": 4210, "出库月均日单量": 140,
        };
        const task = startPreviewTask("kec_reconciliation", {
          is_preview: true,
          success: true,
          output_file: "C:\\Preview\\KEC对账_2026-02_核对结果.xlsx",
          avg_daily_orders: 140, outbound_base_fee: 0.75,
          storage_billed_amount: 21603.09, storage_expected_amount: 19694.02, storage_difference: 1909.07,
          summary,
        });
        task.context = { ...payload };
        task.logs = ["演示模式：未读取真实账单，未登录马帮，未生成 Excel。", "仓租费核算基数：实际体积（长 × 宽 × 高 × 数量 × 0.000001）。"];
        return task;
      },
      async open_path() { return { ok: true }; },
      async reveal_path() { return { ok: true }; },
      async get_vietnam_collection_info() { return { ok: true, period: "2026-05", output_dir: "C:\\Users\\Demo\\Desktop", milestone: "A", real_collection_enabled: false }; },
      async create_vietnam_collection_run(payload) {
        const reportKeys = ["income", "ads", "affiliate", "ads_credit"];
        const stores = (payload.stores || []).map((store) => ({
          store_name: store.name,
          store_type: String(store.name || "").includes("仓发") ? "warehouse" : "normal",
          reports: Object.fromEntries(reportKeys.map((key) => [key, { status: "pending" }])),
        }));
        const runId = `vn_${String(payload.period || "").replace("-", "")}_preview`;
        previewVietnamManifest = { schema_version: 1, run_id: runId, period: payload.period, status: "created", stores };
        return {
          ok: true,
          run_id: runId,
          run_dir: `C:\\Preview\\越南收支报表\\${payload.period}\\${runId}`,
          manifest_path: `C:\\Preview\\越南收支报表\\${payload.period}\\${runId}\\manifest.json`,
          manifest: previewVietnamManifest,
        };
      },
      async get_vietnam_collection_run() { return previewVietnamManifest ? { ok: true, manifest: previewVietnamManifest } : { ok: false, error: "预览模式未写入清单" }; },
      async get_vietnam_report_dashboard() {
        const stateMap = { running: "running", validation_failed: "blocked", ready_to_generate: "ready_to_generate", final_ready: "ready_with_issues" };
        const state = stateMap[previewState] || (previewVietnamManifest?.status === "final_ready" ? "ready_with_issues" : previewVietnamManifest ? "draft" : "empty");
        const finalReady = state === "ready_with_issues";
        const stores = finalReady ? [
          { store_name: "越南店铺 A", order_count: 680, adjusted_receivable: "485220.35", total_expense: "301120.15", actual_refund: "18220.20", final_ads: "80520.10", actual_affiliate: "24500", profit: "184100.20", profit_rate: "0.3794", status: "normal" },
          { store_name: "越南店铺 B", order_count: 510, adjusted_receivable: "360001.02", total_expense: "332102.42", actual_refund: "22100", final_ads: "91520", actual_affiliate: "27800", profit: "27898.60", profit_rate: "0.0775", status: "exception" },
        ] : [];
        const totals = finalReady ? { order_count: 1190, adjusted_receivable: "845221.37", total_expense: "633222.57", actual_refund: "40320.20", final_ads: "172040.10", actual_affiliate: "52300", profit: "211998.80", profit_rate: "0.2508" } : {};
        const exceptions = state === "blocked" ? [{ id: "validation", severity: "blocking", category: "mabang_validation", source: "mabang", title: "马帮收支明细与汇总不一致", message: "订单数差异 2，应收货款差异 158.20 RMB。", suggestion: "请重新导出收支并校验。", store_name: "" }] : finalReady ? [{ id: "unmatched", severity: "attention", category: "unmatched_record", source: "unmatched", title: "未匹配退款", message: "有一条退款未匹配店铺。", suggestion: "请复核未匹配清单。", store_name: "越南店铺 B" }] : [];
        return { ok: true, manifest_path: previewManifestPath, run_dir: "C:\\Preview\\越南收支核对\\2026-05\\vn_202605_preview", period: "2026-05", state, generated_at: finalReady ? "2026-07-11 10:20:00" : "", data_progress: { completed: finalReady ? 14 : 4, total: 14, failed: state === "blocked" ? 1 : 0 }, totals, stores, exceptions, exception_summary: { total_count: exceptions.length, blocking_count: state === "blocked" ? 1 : 0, affected_store_count: finalReady ? 1 : 0, amount: "0" }, required_profit_rate_stores: [], config: previewVietnamManifest?.config || { profit_rates: {} }, summary: { average_exchange_rate: "0.00029", mabang_detail_row_count: 12000, refund_row_count: 18020 }, files: { output_file: finalReady ? "C:\\Preview\\越南Shopee收支报表.xlsx" : "", output_exists: finalReady, unmatched_file: finalReady ? "C:\\Preview\\unmatched.csv" : "", unmatched_exists: finalReady, processing_log: finalReady ? "C:\\Preview\\processing.json" : "", run_dir: "C:\\Preview" } };
      },
      async list_vietnam_report_runs() { return { ok: true, runs: previewVietnamManifest ? [{ run_id: previewVietnamManifest.run_id, period: "2026-05", manifest_path: previewManifestPath, status: previewVietnamManifest.status, store_count: 2, updated_at: "2026-07-11 10:20:00" }] : [] }; },
      async get_vietnam_report_anomalies() { const dashboard = await this.get_vietnam_report_dashboard(); return { ok: true, items: dashboard.exceptions, total: dashboard.exceptions.length }; },
      async save_vietnam_report_config(payload) { if (previewVietnamManifest) previewVietnamManifest.config = { profit_rates: payload.profit_rates || {} }; return { ok: true, config: previewVietnamManifest?.config || {} }; },
      async start_vietnam_subsidy_collection() {
        if (previewVietnamManifest) {
          previewVietnamManifest.stores.forEach((store) => { store.reports.income.status = "success"; store.reports.income.row_count = 40; store.reports.income.subsidy_count = 2; });
          previewVietnamManifest.status = "income_ready";
        }
        return startPreviewTask("vietnam_subsidy_collection", { success_count: 2, failed_count: 0, order_count: 120, subsidy_count: 8, manifest_path: "C:\\Preview\\manifest.json", run_dir: "C:\\Preview" });
      },
      async start_vietnam_ziniao_sources_collection() {
        if (previewVietnamManifest) {
          previewVietnamManifest.stores.forEach((store) => {
            store.reports.ads.status = "success";
            store.reports.ads.row_count = 30;
            store.reports.affiliate.status = "success";
            store.reports.affiliate.row_count = 18;
            store.reports.ads_credit.status = "success";
            store.reports.ads_credit.row_count = 3;
          });
          previewVietnamManifest.status = "sources_ready";
        }
        return startPreviewTask("vietnam_ziniao_sources_collection", { success_count: 6, failed_count: 0, file_count: 6, manifest_path: "C:\\Preview\\manifest.json", run_dir: "C:\\Preview" });
      },
      async start_vietnam_mabang_income_collection() {
        if (previewVietnamManifest) {
          previewVietnamManifest.mabang = {
            status: "income_ready",
            shop_count: 150,
            reports: {
              income_detail: { status: "success", row_count: 12000 },
              income_summary: { status: "success", row_count: 150 },
            },
            validation: {
              status: "passed",
              detail_order_count: 11980,
              summary_order_count: 11980,
              order_count_diff: 0,
              detail_receivable: "845221.37",
              summary_receivable: "845221.37",
              receivable_diff: "0",
            },
          };
          previewVietnamManifest.status = "mabang_income_ready";
        }
        return startPreviewTask("vietnam_mabang_income_collection", { validation_passed: true, detail_order_count: 11980, summary_order_count: 11980, manifest_path: "C:\\Preview\\manifest.json", run_dir: "C:\\Preview" });
      },
      async start_vietnam_mabang_refund_collection() {
        if (previewVietnamManifest?.mabang) {
          Object.assign(previewVietnamManifest.mabang.reports, {
            refund_part_01_10: { status: "success", row_count: 6282 },
            refund_part_11_20: { status: "success", row_count: 5710 },
            refund_part_21_end: { status: "success", row_count: 6034 },
            refunds_merged: { status: "success", row_count: 18020, duplicate_count: 6 },
          });
          previewVietnamManifest.mabang.status = "ready";
          previewVietnamManifest.status = "mabang_ready";
        }
        return startPreviewTask("vietnam_mabang_refund_collection", { merged_row_count: 18020, duplicate_count: 6, manifest_path: "C:\\Preview\\manifest.json", run_dir: "C:\\Preview" });
      },
      async start_vietnam_final_reconciliation() {
        if (previewVietnamManifest) {
          previewVietnamManifest.final = {
            status: "ready",
            output_file: "output\\final\\越南Shopee收支核对_2026-05_preview.xlsx",
            unmatched_count: 2,
            average_exchange_rate: "0.00029",
          };
          previewVietnamManifest.status = "final_ready";
        }
        return startPreviewTask("vietnam_final_reconciliation", { output_file: "C:\\Preview\\越南Shopee收支核对_2026-05_preview.xlsx", store_summary_count: 12, unmatched_count: 2, manifest_path: "C:\\Preview\\manifest.json", run_dir: "C:\\Preview" });
      },
      async get_shopee_ads_info() { return { ok: true, sites: [{ code: "id", name: "印尼", label: "印尼" }, { code: "th", name: "泰国", label: "泰国" }, { code: "ph", name: "菲律宾", label: "菲律宾" }, { code: "vn", name: "越南", label: "越南" }, { code: "my", name: "马来", label: "马来" }], client_path: "C:\\Program Files\\Ziniao\\ziniao.exe", webdriver_path: "C:\\ziniaodriver" }; },
      async save_shopee_ads_config() { return { ok: true }; },
      async choose_client_path() { return { ok: true, path: "C:\\Program Files\\Ziniao\\ziniao.exe" }; },
      async choose_driver_path() { return { ok: true, path: "C:\\ziniaodriver" }; },
      async check_for_updates() { return { ok: true, configured: false }; },
      async get_sample_registration_config() { return { ok: true, config: { version: 1, target_doc_id: "", enable_target_update: false, dingtalk_operator_name: "", output_dir: "", groups: [] } }; },
      async save_sample_registration_config(payload) { return { ok: true, config: payload }; },
      async start_sample_store_match() { return startPreviewTask("sample_registration_match", { resolved_count: 5, unresolved_count: 1, resolved_rows: [], unresolved_rows: [] }); },
      async start_sample_registration() { return startPreviewTask("sample_registration", { processed_count: 120, online_append_count: 100, unmatched_store_count: 0, resolved_count: 10, group_count: 3, output_directory: "C:\\Preview\\寄样", output_files: [] }); },
    };
  }

  return {
    $,
    api,
    state,
    boot,
    activeAccount,
    vendorAccounts,
    applyAccountState,
    showToast,
    appendLog,
    startTask,
    restoreLatestTaskFromKeys,
    openOutput,
    openAccountDialog,
  };
})();
