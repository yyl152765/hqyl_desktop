(() => {
  const $ = HQYL.$;
  let outputDir = "";
  let outputFile = "";
  let filterCategories = [];

  const RANGE_FIELDS = [
    { prefix: "productSales", label: () => `近 ${$("salesPeriodSelect").value} 天商品销量` },
    { prefix: "productTotalSales", label: () => "商品累计销量" },
    { prefix: "productViews", label: () => "商品视频播放量" },
    { prefix: "productVideoCount", label: () => "商品关联视频数" },
    { prefix: "productCreatorCount", label: () => "商品关联达人数" },
    { prefix: "creatorSales", label: () => "达人销量" },
    { prefix: "creatorViews", label: () => "达人视频播放量" },
    { prefix: "creatorFans", label: () => "达人粉丝数" },
    { prefix: "creatorVideoCount", label: () => "达人关联视频数" },
    { prefix: "creatorGmv", label: () => "当前商品带货 GMV" },
  ];

  async function chooseOutputDir() {
    const result = await HQYL.api().choose_output_dir($("outputDir").value || outputDir);
    if (result.ok) {
      outputDir = result.path;
      $("outputDir").value = result.path;
    } else if (!result.cancelled) {
      HQYL.showToast(result.error || "选择目录失败");
    }
  }

  function pageSize() {
    const value = Number($("pageSizeInput").value || 50);
    return Math.max(1, Math.min(50, value || 50));
  }

  function maxProducts() {
    const input = $("maxProductsInput");
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < 1 || value > 10000) {
      const error = new Error("每个搜索范围最多商品数必须是 1 到 10000 之间的整数");
      error.element = input;
      throw error;
    }
    return value;
  }

  function keywords() {
    const seen = new Set();
    return $("keywordInput").value
      .split(/[\r\n,，;；]+/)
      .map((value) => value.trim())
      .filter((value) => {
        const key = value.toLocaleLowerCase();
        if (!value || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  function updateKeywordCount() {
    const count = keywords().length;
    $("keywordCount").textContent = count ? `${count} 个关键词` : "全部商品模式";
  }

  function parseMetricValue(value) {
    const text = String(value || "").replace(/[,，\s]/g, "").trim();
    if (!text) return null;
    const match = text.match(/^([+]?(?:\d+(?:\.\d+)?|\.\d+))([万亿kKmMbB]?)$/);
    if (!match) return Number.NaN;
    const multipliers = { "": 1, 万: 1e4, 亿: 1e8, k: 1e3, m: 1e6, b: 1e9 };
    return Number(match[1]) * multipliers[match[2].toLocaleLowerCase()];
  }

  function readMetricInput(id, label) {
    const input = $(id);
    const raw = input.value.trim();
    if (!raw) return null;
    const value = parseMetricValue(raw);
    if (!Number.isFinite(value) || value < 0) {
      const error = new Error(`${label}格式不正确，请输入非负数字，也可以使用“万、亿、K、M”`);
      error.element = input;
      throw error;
    }
    return value;
  }

  function readRange(prefix, label) {
    const minimum = readMetricInput(`${prefix}Min`, `${label}最小值`);
    const maximum = readMetricInput(`${prefix}Max`, `${label}最大值`);
    if (minimum !== null && maximum !== null && minimum > maximum) {
      const error = new Error(`${label}最小值不能大于最大值`);
      error.element = $(`${prefix}Min`);
      throw error;
    }
    return { min: minimum, max: maximum };
  }

  function selectedCategories(containerId) {
    const selected = [...$(containerId).querySelectorAll('input[type="checkbox"]:checked')];
    return {
      ids: selected.map((input) => input.dataset.categoryId || "").filter(Boolean),
      names: selected.map((input) => input.dataset.categoryName || "").filter(Boolean),
      paths: selected.map((input) => {
        try {
          const path = JSON.parse(input.dataset.categoryPathIds || "[]");
          return Array.isArray(path) ? path.filter(Boolean) : [];
        } catch (_error) {
          return input.dataset.categoryId ? [input.dataset.categoryId] : [];
        }
      }).filter((path) => path.length),
    };
  }

  function buildFilters() {
    const products = selectedCategories("productCategoryOptions");
    const creators = selectedCategories("creatorCategoryOptions");
    return {
      products: {
        category_ids: products.ids,
        category_names: products.names,
        category_paths: products.paths,
        sales_period_days: Number($("salesPeriodSelect").value || 7),
        period_sales: readRange("productSales", "商品周期销量"),
        total_sales: readRange("productTotalSales", "商品累计销量"),
        video_views: readRange("productViews", "商品视频播放量"),
        video_count: readRange("productVideoCount", "商品关联视频数"),
        creator_count: readRange("productCreatorCount", "商品关联达人数"),
      },
      creators: {
        category_names: creators.names,
        sales: readRange("creatorSales", "达人销量"),
        video_play_count: readRange("creatorViews", "达人视频播放量"),
        fans_count: readRange("creatorFans", "达人粉丝数"),
        video_count: readRange("creatorVideoCount", "达人关联视频数"),
        product_gmv: readRange("creatorGmv", "当前商品带货 GMV"),
      },
    };
  }

  function hasProductFilters(filters) {
    const products = filters.products;
    const ranges = [
      products.period_sales,
      products.total_sales,
      products.video_views,
      products.video_count,
      products.creator_count,
    ];
    return Boolean(
      products.category_ids.length
      || ranges.some((range) => range.min !== null || range.max !== null)
    );
  }

  function formatMetric(value) {
    if (value === null || value === undefined) return "";
    if (value >= 1e8) return `${Number((value / 1e8).toFixed(2))}亿`;
    if (value >= 1e4) return `${Number((value / 1e4).toFixed(2))}万`;
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
  }

  function describeRange(label, range) {
    if (range.min !== null && range.max !== null) return `${label} ${formatMetric(range.min)}-${formatMetric(range.max)}`;
    if (range.min !== null) return `${label} ≥${formatMetric(range.min)}`;
    if (range.max !== null) return `${label} ≤${formatMetric(range.max)}`;
    return "";
  }

  function filterSummary(filters) {
    const parts = [];
    if (filters.products.category_names.length) parts.push(`商品类目 ${filters.products.category_names.length} 个`);
    [
      [`近${filters.products.sales_period_days}天销量`, filters.products.period_sales],
      ["累计销量", filters.products.total_sales],
      ["商品播放", filters.products.video_views],
      ["商品视频数", filters.products.video_count],
      ["商品达人数", filters.products.creator_count],
    ].forEach(([label, range]) => {
      const text = describeRange(label, range);
      if (text) parts.push(text);
    });
    if (filters.creators.category_names.length) parts.push(`达人类目 ${filters.creators.category_names.length} 个`);
    [
      ["达人销量", filters.creators.sales],
      ["达人播放", filters.creators.video_play_count],
      ["粉丝数", filters.creators.fans_count],
      ["达人视频数", filters.creators.video_count],
      ["商品 GMV", filters.creators.product_gmv],
    ].forEach(([label, range]) => {
      const text = describeRange(label, range);
      if (text) parts.push(text);
    });
    return parts.length ? parts.join(" · ") : "未设置筛选，将保留全部匹配商品和达人";
  }

  function updateFilterSummary() {
    try {
      $("filterSummaryText").textContent = filterSummary(buildFilters());
    } catch (_error) {
      $("filterSummaryText").textContent = "筛选数值尚未填写完整或格式不正确";
    }
  }

  function categoryChildren(category) {
    return Array.isArray(category?.children)
      ? category.children.filter((child) => child && child.id && child.name)
      : [];
  }

  function createCategoryOption(category, { labelText = category.name, title = category.name, className = "", pathIds = [category.id] } = {}) {
    const label = document.createElement("label");
    label.className = `category-option ${className}`.trim();
    label.title = title;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.categoryId = category.id;
    input.dataset.categoryName = category.name;
    input.dataset.categoryPath = title;
    input.dataset.categoryPathIds = JSON.stringify(pathIds);
    const text = document.createElement("span");
    text.textContent = labelText;
    label.append(input, text);
    return { label, input };
  }

  function countSecondaryCategories(categories) {
    return categories.reduce((total, category) => total + categoryChildren(category).length, 0);
  }

  function renderCategoryOptions(containerId, categories) {
    const container = $(containerId);
    container.textContent = "";
    if (!categories.length) {
      const empty = document.createElement("span");
      empty.className = "category-empty";
      empty.textContent = "暂无可用类目，可不选择类目继续采集";
      container.appendChild(empty);
      return;
    }
    categories.forEach((category) => {
      const children = categoryChildren(category);
      if (!children.length) {
        container.appendChild(createCategoryOption(category).label);
        return;
      }

      const group = document.createElement("details");
      group.className = "category-tree-group";
      const summary = document.createElement("summary");
      summary.className = "category-tree-summary";
      summary.title = category.name;
      const summaryName = document.createElement("span");
      summaryName.textContent = category.name;
      const childCount = document.createElement("small");
      childCount.textContent = `${children.length} 个二级类目`;
      summary.append(summaryName, childCount);

      const childOptions = document.createElement("div");
      childOptions.className = "category-tree-children";
      const parentOption = createCategoryOption(category, {
        labelText: `全部 ${category.name}`,
        title: category.name,
        className: "category-option-all",
        pathIds: [category.id],
      });
      const childInputs = [];
      childOptions.appendChild(parentOption.label);

      children.forEach((child) => {
        const path = `${category.name} / ${child.name}`;
        const childOption = createCategoryOption(child, {
          title: path,
          className: "category-option-secondary",
          pathIds: [category.id, child.id],
        });
        childOption.input.addEventListener("change", () => {
          if (childOption.input.checked) parentOption.input.checked = false;
        });
        childInputs.push(childOption.input);
        childOptions.appendChild(childOption.label);
      });

      parentOption.input.addEventListener("change", () => {
        if (parentOption.input.checked) {
          childInputs.forEach((input) => {
            input.checked = false;
          });
        }
      });

      group.append(summary, childOptions);
      container.appendChild(group);
    });
  }

  async function loadFilterOptions() {
    const account = HQYL.activeAccount("echotik");
    if (!account) {
      $("filterOptionsStatus").textContent = "绑定账号后加载类目";
      $("filterOptionsStatus").className = "filter-load-status";
      renderCategoryOptions("productCategoryOptions", []);
      renderCategoryOptions("creatorCategoryOptions", []);
      return;
    }
    $("filterOptionsStatus").textContent = "正在加载类目…";
    $("filterOptionsStatus").className = "filter-load-status";
    try {
      const result = await HQYL.api().get_echotik_filter_options({ account_id: account.id });
      if (!result.ok) throw new Error(result.error || "类目加载失败");
      filterCategories = Array.isArray(result.categories) ? result.categories : [];
      renderCategoryOptions("productCategoryOptions", filterCategories);
      renderCategoryOptions("creatorCategoryOptions", filterCategories);
      const secondaryCount = countSecondaryCategories(filterCategories);
      $("filterOptionsStatus").textContent = secondaryCount
        ? `已加载 ${filterCategories.length} 个一级类目 · ${secondaryCount} 个二级类目`
        : `已加载 ${filterCategories.length} 个类目`;
      $("filterOptionsStatus").className = "filter-load-status success";
    } catch (error) {
      filterCategories = [];
      renderCategoryOptions("productCategoryOptions", []);
      renderCategoryOptions("creatorCategoryOptions", []);
      $("filterOptionsStatus").textContent = "类目加载失败，可不选类目继续";
      $("filterOptionsStatus").className = "filter-load-status failed";
      HQYL.appendLog(`EchoTik 类目加载失败：${error.message || error}`);
    }
  }

  function resetFilters() {
    RANGE_FIELDS.forEach(({ prefix }) => {
      $(`${prefix}Min`).value = "";
      $(`${prefix}Max`).value = "";
    });
    $("salesPeriodSelect").value = "7";
    ["productCategoryOptions", "creatorCategoryOptions"].forEach((id) => {
      $(id).querySelectorAll('input[type="checkbox"]').forEach((input) => {
        input.checked = false;
      });
      $(id).querySelectorAll("details").forEach((details) => {
        details.open = false;
      });
    });
    updateFilterSummary();
  }

  async function runCollection() {
    const keywordList = keywords();
    let filters;
    let productLimit;
    try {
      filters = buildFilters();
      productLimit = maxProducts();
    } catch (error) {
      HQYL.showToast(error.message || String(error));
      error.element?.focus();
      return;
    }
    if (
      !keywordList.length
      && !hasProductFilters(filters)
      && !window.confirm(
        `当前未填写关键词，也未设置商品筛选，将从全部商品榜单采集最多 ${productLimit} 个商品，`
        + "并逐个采集达人，可能耗时较长。确认继续吗？"
      )
    ) {
      return;
    }
    const account = HQYL.activeAccount("echotik");
    if (!account) {
      HQYL.openAccountDialog("echotik", async () => {
        await loadFilterOptions();
        await runCollection();
      });
      return;
    }
    await HQYL.startTask(() => {
      const scopeLabel = keywordList.length ? `${keywordList.length} 个关键词` : "全部商品（无关键词）";
      HQYL.appendLog(
        `准备启动 EchoTik 商品达人采集；范围：${scopeLabel}；`
        + `每个范围最多 ${productLimit} 个商品；${filterSummary(filters)}`
      );
      return HQYL.api().start_echotik_collection({
        account_id: account.id,
        keywords: keywordList,
        output_dir: outputDir,
        page_size: pageSize(),
        max_products: productLimit,
        filters,
      });
    });
  }

  function renderPreview(rows) {
    const tbody = $("resultTableBody");
    tbody.textContent = "";
    (rows || []).forEach((row) => {
      const tr = document.createElement("tr");
      [
        row.keyword,
        row.product_id,
        row.product_title,
        row.creator_id,
        row.creator_name,
        row.creator_categories,
        row.creator_sales,
        row.fans_count,
        row.video_play_count,
        row.product_gmv,
        row.video_count,
      ].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value === undefined || value === null || value === "" ? "—" : String(value);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    $("resultEmpty").hidden = Boolean(rows && rows.length);
    $("resultTableWrap").hidden = !(rows && rows.length);
  }

  const page = {
    key: "echotik_collect",
    taskKey: "echotik_collect",
    title: "EchoTik 商品达人采集",
    async init(info) {
      const settings = info.settings || {};
      outputDir = settings.output_dir || "";
      $("outputDir").value = outputDir;
      $("echotikCollectForm").addEventListener("submit", (event) => {
        event.preventDefault();
        runCollection();
      });
      $("keywordInput").addEventListener("input", updateKeywordCount);
      $("echotikCollectForm").addEventListener("input", updateFilterSummary);
      $("echotikCollectForm").addEventListener("change", updateFilterSummary);
      $("resetFiltersBtn").addEventListener("click", resetFilters);
      $("chooseOutputBtn").addEventListener("click", chooseOutputDir);
      $("openOutputBtn").addEventListener("click", () => HQYL.openOutput(outputFile || outputDir));
      updateKeywordCount();
      updateFilterSummary();
      await loadFilterOptions();
    },
    setRunning(running) {
      $("echotikCollectForm").querySelectorAll("input, textarea, select, button").forEach((element) => {
        if (element.id !== "openOutputBtn") element.disabled = running;
      });
      $("openOutputBtn").disabled = running || (!outputFile && !outputDir);
    },
    resetResult() {
      $("skuCount").textContent = "0";
      $("rawCreatorCount").textContent = "0";
      $("recordCount").textContent = "0";
      $("filteredCreatorText").textContent = "排除 0 条";
      $("outputFile").textContent = "未生成";
      $("failedProductText").textContent = "失败商品 0";
      $("filteredProductText").textContent = "候选商品 0";
      $("openOutputBtn").disabled = true;
      outputFile = "";
      renderPreview([]);
    },
    applyResult(result) {
      const sourceCreators = result.creator_source_count ?? result.creator_count ?? 0;
      const retainedCreators = result.creator_count ?? 0;
      const filteredCreators = result.creator_filtered_count ?? Math.max(0, sourceCreators - retainedCreators);
      const missing = result.creator_missing_metric_count ?? 0;
      $("skuCount").textContent = String(result.product_count ?? 0);
      $("rawCreatorCount").textContent = String(sourceCreators);
      $("recordCount").textContent = String(retainedCreators);
      $("filteredCreatorText").textContent = `排除 ${filteredCreators} 条${missing ? ` · 指标缺失 ${missing}` : ""}`;
      $("filteredProductText").textContent = `候选商品 ${result.product_candidate_count ?? result.product_count ?? 0}`;
      $("failedProductText").textContent = `失败商品 ${result.failed_product_count ?? 0}`;
      outputFile = result.output_file || "";
      outputDir = result.output_dir || outputDir;
      $("outputFile").textContent = outputFile || "未生成";
      $("openOutputBtn").disabled = !outputFile && !outputDir;
      renderPreview(result.creators_preview || []);
    },
  };

  HQYL.boot(page);
})();
