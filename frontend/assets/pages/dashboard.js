(() => {
  const $ = HQYL.$;
  const TOOL_META = {
    temu_on_sale_export: { href: "temu-on-sale-export.html", icon: "导", featured: true },
    temu_balance_statistics: { href: "temu-balance-statistics.html", icon: "余", featured: true },
    lazada_balance_statistics: { href: "lazada-balance-statistics.html", icon: "余", featured: true },
    purchase_log: { href: "purchase-log.html", icon: "SKU" },
    mabang_arrival_query: { href: "mabang-arrival-query.html", icon: "到", featured: true },
    sku_inventory_query: { href: "sku-inventory-query.html", icon: "存", featured: true },
    developer_sales_income_summary: { href: "developer-sales-income-summary.html", icon: "汇", featured: true },
    mabang_income_expense_report: { href: "mabang-income-expense-report.html", icon: "收", featured: true },
    temu_shipping_channel: { href: "temu-shipping-channel.html", icon: "运", featured: true },
    ph_group_sales_report: { href: "group-sales.html", icon: "销" },
    shopee_ads: { href: "shopee-ads.html", icon: "充" },
    lazada_withdrawal_statistics: { href: "lazada-withdrawal-statistics.html", icon: "提" },
    lazada_monthly_report: { href: "lazada-monthly-report.html", icon: "月", featured: true },
    bigseller_sync: { href: "bigseller-sync.html", icon: "同" },
    bigseller_item_id_query: { href: "bigseller-item-id-query.html", icon: "ID", featured: true },
    bigseller_sku_benchmark: { href: "bigseller-sku-benchmark.html", icon: "标", featured: true },
    bigseller_claim_query: { href: "bigseller-claim-query.html", icon: "新", featured: true },
    sample_registration: { href: "sample-registration.html", icon: "样" },
    mabang_warehouse_permission: { href: "mabang-warehouse-permission.html", icon: "仓", featured: true },
    mabang_developer_permission: { href: "mabang-developer-permission.html", icon: "开", featured: true },
    echotik_collect: { href: "echotik-collect.html", icon: "采" },
    kec_reconciliation: { href: "kec-reconciliation.html", icon: "K", featured: true },
  };
  const VENDORS = {
    mabang: { label: "马帮 ERP", mark: "M" },
    ziniao: { label: "紫鸟浏览器", mark: "Z" },
    bigseller: { label: "BigSeller", mark: "B" },
    echotik: { label: "EchoTik", mark: "E" },
  };
  const PREVIEW_TOOLS = [
    { key: "temu_on_sale_export", name: "TEMU 在售商品导出", description: "自定义选店、原文件归档、全部在售 SKU 汇总与失败重试。", vendor: "ziniao" },
    { key: "temu_balance_statistics", name: "TEMU 余额统计", description: "按所选月份采集待处理款项，保存当前账户总金额和页面截图。", vendor: "ziniao" },
    { key: "lazada_balance_statistics", name: "Lazada 余额统计", description: "多站点采集当前 Income、Balance、Ads 与处理中提现，并保存截图。", vendor: "ziniao" },
    { key: "mabang_arrival_query", name: "到货查询与导出", description: "批量按备注查询调拨批次并导出到货明细 Excel。", vendor: "mabang" },
    { key: "mabang_warehouse_permission", name: "马帮仓库权限批量开通", description: "批量匹配员工并新增指定仓库的数据权限。", vendor: "mabang" },
    { key: "mabang_developer_permission", name: "马帮批量添加开发员", description: "按员工名单添加开发员岗位，设置按商品父目录查看并保存。", vendor: "mabang" },
    { key: "sku_inventory_query", name: "SKU库存与可售天数查询", description: "按开发员查询爆款旺款缺失的东南亚仓库，或查看库存、未发货和在途明细。", vendor: "mabang" },
    { key: "developer_sales_income_summary", name: "开发与销售收入汇总导出", description: "按付款日期和开发员名单汇总开发、销售收入订单金额（美元）。", vendor: "mabang" },
    { key: "mabang_income_expense_report", name: "马帮收支报表", description: "按店铺自定义分类、海外仓和发货时间导出收支明细 CSV。", vendor: "mabang" },
    { key: "temu_shipping_channel", name: "TEMU发货渠道更改", description: "导入每日随机长宽高 Excel，按顺序设置已开启的 TEMU 渠道并核验，关闭渠道不占用 Excel 行。", vendor: "mabang" },
    { key: "ph_group_sales_report", name: "菲律宾各组商品销量报表", description: "按小组和时间范围导出马帮商品销量报表。", vendor: "mabang" },
    { key: "purchase_log", name: "SKU 采购日志查询", description: "按 SKU 查询马帮采购日志并导出 Excel。", vendor: "mabang" },
    { key: "sample_registration", name: "网红寄样登记", description: "查询样品订单、导出 Excel、同步钉钉在线表格。", vendor: "mabang" },
    { key: "shopee_ads", name: "Shopee 广告充值", description: "多站点店铺广告充值，支持印尼、泰国、菲律宾、越南、马来。", vendor: "ziniao" },
    { key: "lazada_withdrawal_statistics", name: "Lazada 提现统计", description: "菲律宾、马来提现流水 Excel 及泰国收入账单归档。", vendor: "ziniao" },
    { key: "lazada_monthly_report", name: "Lazada 月度账单下载", description: "按国家、月份和店铺批量下载月度报告，并以店铺名称保存。", vendor: "ziniao" },
    { key: "bigseller_sync", name: "BigSeller 同步", description: "通过 BigSeller 接口同步产品或库存，支持在售、售完状态。", vendor: "bigseller" },
    { key: "bigseller_item_id_query", name: "BS 商品ID查询", description: "SKU（含子SKU）模糊搜索，取 Views 降序首条 Item ID 并导出 Excel。", vendor: "bigseller" },
    { key: "bigseller_claim_query", name: "新品认领时间查询", description: "导入 Excel，查询主 SKU 最早创建商品的店铺和两个时间。", vendor: "bigseller" },
    { key: "bigseller_sku_benchmark", name: "滞销SKU爆款对标", description: "导入滞销 SKU 表，按浏览量或销量比较各店最高单商品指标并导出 Excel。", vendor: "bigseller" },
    { key: "echotik_collect", name: "EchoTik 商品达人采集", description: "固定泰国站，按关键词采集商品库和商品达人列表。", vendor: "echotik" },
    { key: "kec_reconciliation", name: "KEC 对账", description: "核对 KEC 账单五项费用，仓租费改以实际体积为核算基数并回查马帮订单。", vendor: "mabang" },
  ];
  let tools = [];
  let selectedVendor = "all";

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character]);
  }

  function visibleTools() {
    const keyword = $("toolSearch").value.trim().toLowerCase();
    return tools.filter((tool) => {
      if (!TOOL_META[tool.key]) return false;
      if (selectedVendor !== "all" && tool.vendor !== selectedVendor) return false;
      return !keyword || `${tool.name} ${tool.description}`.toLowerCase().includes(keyword);
    });
  }

  function renderTools() {
    const filtered = visibleTools();
    const groups = Object.keys(VENDORS).map((vendor) => ({ vendor, items: filtered.filter((tool) => tool.vendor === vendor) })).filter((group) => group.items.length);
    $("toolGroups").innerHTML = groups.map((group) => {
      const vendor = VENDORS[group.vendor];
      const cards = group.items.map((tool) => {
        const meta = TOOL_META[tool.key];
        const account = HQYL.activeAccount(tool.vendor);
        const availability = account ? `账号：${escapeHtml(account.name || account.username)}` : "进入后绑定账号";
        return `<a class="dashboard-tool${meta.featured ? " featured" : ""}" href="${meta.href}">
          <span class="dashboard-tool-icon">${meta.icon}</span>
          <span class="dashboard-tool-content"><span class="dashboard-tool-title">${escapeHtml(tool.name)}${meta.featured ? '<em>新功能</em>' : ""}</span><span class="dashboard-tool-description">${escapeHtml(tool.description)}</span><span class="dashboard-tool-account">${availability}</span></span>
          <span class="dashboard-tool-arrow">›</span>
        </a>`;
      }).join("");
      return `<section class="dashboard-group" data-vendor="${group.vendor}"><div class="dashboard-group-head"><span class="dashboard-vendor-mark">${vendor.mark}</span><div><h2>${vendor.label}</h2><p>${group.items.length} 个工具</p></div></div><div class="dashboard-tool-grid">${cards}</div></section>`;
    }).join("");
    $("toolEmpty").hidden = filtered.length > 0;
  }

  function bindFilters() {
    $("vendorTabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-vendor]");
      if (!button) return;
      selectedVendor = button.dataset.vendor;
      document.querySelectorAll(".dashboard-tab").forEach((tab) => tab.classList.toggle("active", tab === button));
      renderTools();
    });
    $("toolSearch").addEventListener("input", renderTools);
  }

  HQYL.boot({
    key: "dashboard",
    title: "工作台",
    async init(info) {
      tools = Array.isArray(info.tools) && info.tools.length ? info.tools : PREVIEW_TOOLS;
      $("boundAccountCount").textContent = String((info.account_state?.accounts || []).length);
      bindFilters();
      renderTools();
    },
    onAccountsChanged() {
      $("boundAccountCount").textContent = String(HQYL.state.accounts.length);
      if (tools.length) renderTools();
    },
  });
})();
