const state = {
  view: "overview",
  dashboard: null,
  factors: [],
  backtests: [],
  instruments: [],
  requirements: null,
  dataStatus: null,
  indices: [],
  paperAccount: null,
  liveGuard: null,
  marketClock: null,
  readiness: null,
  strategies: [],
  strategyRuns: [],
  activeStrategyRun: null,
  activeBacktest: null,
  explorer: {
    tab: "prices",
    symbol: "600519.SH",
    prices: null,
    financials: null,
    factorId: "",
    factorScores: null,
    screenIndustry: "",
    screenKeyword: "",
    screenSort: "symbol",
  },
};

const viewMeta = {
  overview: ["WORKSPACE / OVERVIEW", "量化研究总览"],
  data: ["RESEARCH / DATA", "数据中心"],
  explorer: ["RESEARCH / EXPLORER", "数据浏览"],
  factors: ["RESEARCH / FACTORS", "因子实验室"],
  backtest: ["RESEARCH / BACKTEST", "策略回测"],
  risk: ["PORTFOLIO / RISK", "风险检查"],
  paper: ["EXECUTION / PAPER", "模拟交易"],
  strategy: ["EXECUTION / STRATEGY", "策略运行"],
  agent: ["RESEARCH / AGENT", "研究 Agent"],
};

const workspace = document.querySelector("#workspace");
const toast = document.querySelector("#toast");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function percent(value, digits = 2) {
  const number = Number(value || 0);
  return `${(number * 100).toFixed(digits)}%`;
}

function number(value, digits = 2) {
  return Number(value || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function showToast(message, type = "info") {
  toast.textContent = message;
  toast.className = `toast show ${type === "error" ? "error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.className = "toast";
  }, 2600);
}

async function api(path, options = {}) {
  const adminKey = window.localStorage.getItem("quantdevAdminKey") || "";
  const readKey = window.localStorage.getItem("quantdevReadKey") || "";
  const operator = window.localStorage.getItem("quantdevOperator") || "local-admin";
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(readKey ? { "X-Read-Key": readKey } : {}),
      ...(adminKey ? { "X-Admin-Key": adminKey } : {}),
      "X-Operator": operator,
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "请求失败");
  }
  return payload;
}

async function waitForTask(taskId, message = "任务执行中") {
  for (;;) {
    const task = await api(`/api/tasks/${taskId}`);
    if (task.status === "COMPLETED") return task.result;
    if (task.status === "FAILED") {
      throw new Error(task.error_message || "任务执行失败");
    }
    showToast(`${message}，第 ${task.attempts || 0} 次尝试`);
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
}

async function loadCoreData() {
  const [
    dashboard,
    factors,
    backtests,
    instruments,
    requirements,
    paperAccount,
    dataStatus,
    indices,
    liveGuard,
    marketClock,
    readiness,
    strategies,
    strategyRuns,
  ] = (
    // 用 allSettled 优雅降级：未输入 admin key 时受保护接口(账户/guard)会 401/503，
    // 不应拖垮整个首屏；失败项回退到安全默认，对应面板单独显示“需认证/暂不可用”。
    await Promise.allSettled([
      api("/api/dashboard"),
      api("/api/factors"),
      api("/api/backtests"),
      api("/api/instruments"),
      api("/api/system/requirements"),
      api("/api/paper/account"),
      api("/api/data/status"),
      api("/api/data/indices"),
      api("/api/live/guard"),
      api("/api/live/market-clock"),
      api("/api/live/readiness"),
      api("/api/strategies"),
      api("/api/strategies/runs"),
    ])
  ).map((r, i) => (r.status === "fulfilled" ? r.value : DASHBOARD_FALLBACKS[i]));
  state.dashboard = dashboard;
  state.factors = factors.items;
  state.backtests = backtests.items;
  state.instruments = instruments.items;
  state.requirements = requirements;
  state.paperAccount = paperAccount;
  state.dataStatus = dataStatus;
  state.indices = indices.items;
  state.liveGuard = liveGuard;
  state.marketClock = marketClock;
  state.readiness = readiness;
  state.strategies = strategies.items;
  state.strategyRuns = strategyRuns.items;
  document.querySelector("#snapshot-label").textContent =
    dashboard.snapshot_id || "暂无数据快照";
}

// 首屏各接口失败时的安全回退（与 Promise.allSettled 顺序一一对应），避免单点 401 拖垮整页。
const DASHBOARD_FALLBACKS = [
  {}, // dashboard
  { items: [] }, // factors
  { items: [] }, // backtests
  { items: [] }, // instruments
  {}, // requirements
  { positions: [], orders: [], cash: null, equity: null, requires_auth: true }, // paper/account
  {}, // data/status
  { items: [] }, // data/indices
  { kill_switch: { active: false }, requires_auth: true }, // live/guard
  {}, // market-clock
  {}, // readiness
  { items: [] }, // strategies
  { items: [] }, // strategies/runs
];

function metric(label, value, note = "", tone = "") {
  return `
    <div class="metric">
      <span class="metric-label">${escapeHtml(label)}</span>
      <strong class="metric-value ${tone}">${escapeHtml(value)}</strong>
      <span class="metric-note">${escapeHtml(note)}</span>
    </div>`;
}

function renderOverview() {
  const latest = state.dashboard.latest_backtest;
  const metrics = latest?.metrics || {};
  workspace.innerHTML = `
    <div class="metric-grid">
      ${metric("研究资产", state.dashboard.instrument_count, "含真实行情的 A 股标的")}
      ${metric("已批准因子", state.dashboard.approved_factor_count, `共 ${state.dashboard.factor_count} 个因子`)}
      ${metric("最新年化收益", percent(metrics.annualized_return), latest?.name || "暂无回测", Number(metrics.annualized_return) >= 0 ? "positive" : "negative")}
      ${metric("最大回撤", percent(metrics.max_drawdown), "成本后回测", "negative")}
    </div>
    <div class="workspace-grid">
      <div>
        <section class="panel">
          <div class="panel-header">
            <div>
              <h2>策略净值</h2>
              <p>${escapeHtml(latest?.name || "暂无回测")}</p>
            </div>
            <span class="badge">${escapeHtml(latest?.factor_id || "WAITING")}</span>
          </div>
          <div class="chart-wrap"><canvas id="overview-chart"></canvas></div>
        </section>
        <section class="panel">
          <div class="panel-header">
            <div><h2>最近实验</h2><p>绑定代码、参数和数据快照</p></div>
          </div>
          ${backtestTable(state.dashboard.recent_backtests)}
        </section>
      </div>
      <aside>
        <section class="panel">
          <div class="panel-header">
            <div><h2>系统状态</h2><p>交易权限隔离</p></div>
          </div>
          <div class="status-list">
            ${state.dashboard.system_status
              .map(
                (item) => `
                <div class="status-row">
                  <span>${escapeHtml(item.name)}</span>
                  <span class="badge ${item.status === "disabled" ? "disabled" : ""}">${escapeHtml(item.status)}</span>
                </div>`,
              )
              .join("")}
          </div>
        </section>
        <section class="panel">
          <div class="panel-header">
            <div><h2>数据健康</h2><p>${escapeHtml(state.dashboard.snapshot_id)}</p></div>
          </div>
          <div class="status-list">
            <div class="status-row"><span>行情记录</span><strong>${state.dashboard.price_row_count.toLocaleString("zh-CN")}</strong></div>
            <div class="status-row"><span>起始日期</span><strong>${escapeHtml(state.dashboard.date_range[0])}</strong></div>
            <div class="status-row"><span>结束日期</span><strong>${escapeHtml(state.dashboard.date_range[1])}</strong></div>
          </div>
        </section>
      </aside>
    </div>`;
  if (latest) {
    loadBacktestChart(latest.run_id, "overview-chart");
  }
}

function backtestTable(items) {
  if (!items.length) return '<div class="empty-state">暂无回测记录</div>';
  return `
    <div class="table-shell">
      <table>
        <thead><tr><th>名称</th><th>因子</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>时间</th></tr></thead>
        <tbody>
          ${items
            .map(
              (item) => `
              <tr>
                <td>${escapeHtml(item.name)}</td>
                <td class="mono">${escapeHtml(item.factor_id)}</td>
                <td class="${item.metrics.annualized_return >= 0 ? "positive" : "negative"}">${percent(item.metrics.annualized_return)}</td>
                <td>${number(item.metrics.sharpe_ratio)}</td>
                <td class="negative">${percent(item.metrics.max_drawdown)}</td>
                <td>${escapeHtml(item.created_at.slice(0, 16).replace("T", " "))}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>
    </div>`;
}

async function loadBacktestChart(runId, canvasId) {
  const detail = await api(`/api/backtests/${runId}`);
  state.activeBacktest = detail;
  const canvas = document.querySelector(`#${canvasId}`);
  if (canvas) drawLineChart(canvas, detail.equity_curve, "equity", "benchmark");
}

function drawLineChart(canvas, points, valueKey, benchmarkKey) {
  if (!points?.length) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  const padding = { left: 58, right: 16, top: 18, bottom: 32 };
  const values = points.map((point) => Number(point[valueKey]));
  const hasBenchmark =
    benchmarkKey && points.every((point) => point[benchmarkKey] != null);
  const benchmarkValues = hasBenchmark
    ? points.map((point) => Number(point[benchmarkKey]))
    : [];
  const allValues = values.concat(benchmarkValues);
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const spread = max - min || 1;
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "#e1e6e3";
  ctx.fillStyle = "#65706d";
  ctx.font = "10px ui-monospace, monospace";
  ctx.lineWidth = 1;
  for (let index = 0; index <= 4; index += 1) {
    const y = padding.top + (plotHeight * index) / 4;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    const label = max - (spread * index) / 4;
    ctx.fillText(Math.round(label).toLocaleString("zh-CN"), 4, y + 3);
  }

  const coordinates = values.map((value, index) => ({
    x: padding.left + (index / Math.max(1, values.length - 1)) * plotWidth,
    y: padding.top + (1 - (value - min) / spread) * plotHeight,
  }));
  const gradient = ctx.createLinearGradient(0, padding.top, 0, height - padding.bottom);
  gradient.addColorStop(0, "rgba(11, 122, 83, 0.22)");
  gradient.addColorStop(1, "rgba(11, 122, 83, 0.01)");
  ctx.beginPath();
  ctx.moveTo(coordinates[0].x, height - padding.bottom);
  coordinates.forEach((point) => ctx.lineTo(point.x, point.y));
  ctx.lineTo(coordinates.at(-1).x, height - padding.bottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  if (hasBenchmark) {
    const benchmarkCoordinates = benchmarkValues.map((value, index) => ({
      x: padding.left + (index / Math.max(1, benchmarkValues.length - 1)) * plotWidth,
      y: padding.top + (1 - (value - min) / spread) * plotHeight,
    }));
    ctx.beginPath();
    benchmarkCoordinates.forEach((point, index) => {
      if (index === 0) ctx.moveTo(point.x, point.y);
      else ctx.lineTo(point.x, point.y);
    });
    ctx.strokeStyle = "#9aa3a0";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.beginPath();
  coordinates.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.strokeStyle = "#0b7a53";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fillStyle = "#65706d";
  ctx.fillText(points[0].date, padding.left, height - 9);
  const endText = points.at(-1).date;
  const endWidth = ctx.measureText(endText).width;
  ctx.fillText(endText, width - padding.right - endWidth, height - 9);
  if (hasBenchmark) {
    ctx.fillStyle = "#0b7a53";
    ctx.fillText("— 策略", padding.left, padding.top - 4);
    ctx.fillStyle = "#9aa3a0";
    ctx.fillText("--- 等权基准", padding.left + 56, padding.top - 4);
  }
}

function renderData() {
  const status = state.dataStatus;
  const requirements = state.requirements.optional_data_apis;
  const providerName =
    status.provider === "tinyshare" ? "Tinyshare 代理接口" : "Tushare Pro";
  const tokenName =
    status.provider === "tinyshare" ? "TINYSHARE_TOKEN" : "TUSHARE_TOKEN";
  const defaultEnd = new Date().toISOString().slice(0, 10);
  const defaultStartDate = new Date();
  defaultStartDate.setFullYear(defaultStartDate.getFullYear() - 1);
  const defaultStart = defaultStartDate.toISOString().slice(0, 10);
  const visibleInstruments = state.instruments.slice(0, 100);
  const visibleIndices = state.indices
    .filter(
      (item) =>
        item.latest_weight_date || status.default_indices.includes(item.index_code),
    )
    .slice(0, 50);
  workspace.innerHTML = `
    <div class="requirement-band ${status.configured ? "configured" : ""}">
      ${
        status.configured
          ? `${providerName}已配置，当前使用 <span class="mono">${escapeHtml(status.financial_mode)}</span> 财务接口。同步任务在后台运行，重复日期会自动跳过。`
          : `尚未配置真实数据权限。请在项目根目录 <span class="mono">.env</span> 写入 <span class="mono">${tokenName}</span> 并重启服务；授权码不会显示在页面或写入数据库。`
      }
    </div>
    <div class="metric-grid">
      ${metric("真实行情", Number(status.prices.row_count).toLocaleString("zh-CN"), `${status.prices.symbols} 个股票`)}
      ${metric("财务指标", Number(status.financial_rows).toLocaleString("zh-CN"), status.financial_mode === "vip" ? "全市场季度拉取" : "按股票拉取")}
      ${metric("指数成分", Number(status.constituent_rows).toLocaleString("zh-CN"), `${status.index_rows} 个指数`)}
      ${metric("最新交易日", status.prices.end_date || "尚未同步", status.snapshot_id)}
    </div>
    <section class="panel" style="margin-top:24px">
      <div class="panel-header">
        <div><h2>同步真实 A 股数据</h2><p>行情、复权因子、每日估值、财务指标和历史指数权重</p></div>
        <span class="badge ${status.configured ? "" : "disabled"}">${status.configured ? escapeHtml(status.provider.toUpperCase()) : "TOKEN REQUIRED"}</span>
      </div>
      <form id="tushare-sync-form">
        <div class="form-grid">
          <div class="field"><label for="sync-start">开始日期</label><input id="sync-start" name="start_date" type="date" value="${defaultStart}" required /></div>
          <div class="field"><label for="sync-end">结束日期</label><input id="sync-end" name="end_date" type="date" value="${defaultEnd}" required /></div>
          <div class="field span-2"><label for="sync-indices">指数代码</label><input id="sync-indices" name="indices" value="${escapeHtml(status.default_indices.join(", "))}" /></div>
          <div class="field span-2"><label for="sync-financial-limit">普通财务接口股票上限</label><input id="sync-financial-limit" name="financial_limit" type="number" value="100" min="1" max="1000" ${status.financial_mode === "vip" ? "disabled" : ""} /></div>
          <div class="field span-2">
            <label>数据模块</label>
            <div class="sync-options">
              <label class="toggle-row"><input name="sync_daily" type="checkbox" checked /><span>日线与每日指标</span></label>
              <label class="toggle-row"><input name="sync_financials" type="checkbox" checked /><span>财务指标</span></label>
              <label class="toggle-row"><input name="sync_indices" type="checkbox" checked /><span>指数与历史成分</span></label>
            </div>
          </div>
        </div>
        <div class="form-actions">
          <button class="button" type="submit" ${status.configured ? "" : "disabled"}>开始同步</button>
        </div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>同步任务</h2><p>最近 10 次执行记录</p></div>
      </div>
      ${syncRunsTable(status.runs)}
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>指数数据</h2><p>展示已配置或已同步成分的指数</p></div>
        <span class="badge">${visibleIndices.length} INDEXES</span>
      </div>
      ${indicesTable(visibleIndices)}
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>研究标的</h2><p>统一证券主数据，页面最多展示前 100 条</p></div>
        <span class="badge">${state.instruments.length} ASSETS</span>
      </div>
      <div class="table-shell">
        <table>
          <thead><tr><th>代码</th><th>名称</th><th>交易所</th><th>资产类型</th><th>行业</th></tr></thead>
          <tbody>${visibleInstruments
            .map(
              (item) => `<tr><td class="mono">${escapeHtml(item.symbol)}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.exchange)}</td><td>${escapeHtml(item.asset_type)}</td><td>${escapeHtml(item.industry)}</td></tr>`,
            )
            .join("")}</tbody>
        </table>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>外部连接</h2><p>可选 API 与权限状态</p></div>
      </div>
      <div class="status-list">
        ${requirements
          .map(
            (item) => `
            <div class="status-row">
              <div><strong>${escapeHtml(item.name)}</strong><div class="subtle">${escapeHtml(item.purpose)}</div></div>
              <span class="badge ${item.status === "configured" ? "" : "disabled"}">${escapeHtml(item.status)}</span>
            </div>`,
          )
          .join("")}
      </div>
    </section>`;
  document
    .querySelector("#tushare-sync-form")
    .addEventListener("submit", startTushareSync);
}

function syncRunsTable(items) {
  if (!items.length) return '<div class="empty-state compact">暂无真实数据同步任务</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>任务</th><th>状态</th><th>区间</th><th>行情</th><th>财务</th><th>指数成分</th><th>开始时间</th></tr></thead>
    <tbody>${items
      .map(
        (item) => `<tr>
          <td class="mono">${escapeHtml(item.run_id)}</td>
          <td><span class="badge ${item.status === "failed" ? "danger" : item.status === "completed" ? "" : "disabled"}">${escapeHtml(item.status)}</span>${item.error_message ? `<div class="subtle sync-error">${escapeHtml(item.error_message)}</div>` : ""}</td>
          <td>${escapeHtml(item.parameters.start_date)} → ${escapeHtml(item.parameters.end_date)}</td>
          <td>${Number(item.stats.price_rows || 0).toLocaleString("zh-CN")}</td>
          <td>${Number(item.stats.financial_rows || 0).toLocaleString("zh-CN")}</td>
          <td>${Number(item.stats.constituent_rows || 0).toLocaleString("zh-CN")}</td>
          <td>${escapeHtml(item.started_at.slice(0, 16).replace("T", " "))}</td>
        </tr>`,
      )
      .join("")}</tbody>
  </table></div>`;
}

function indicesTable(items) {
  if (!items.length) return '<div class="empty-state compact">同步后显示指数和最新成分日期</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>指数代码</th><th>名称</th><th>市场</th><th>发布方</th><th>最新权重日</th><th>成分数</th></tr></thead>
    <tbody>${items
      .map(
        (item) => `<tr>
          <td class="mono">${escapeHtml(item.index_code)}</td>
          <td>${escapeHtml(item.name)}</td>
          <td>${escapeHtml(item.market || "-")}</td>
          <td>${escapeHtml(item.publisher || "-")}</td>
          <td>${escapeHtml(item.latest_weight_date || "-")}</td>
          <td>${Number(item.latest_constituent_count || 0).toLocaleString("zh-CN")}</td>
        </tr>`,
      )
      .join("")}</tbody>
  </table></div>`;
}

async function startTushareSync(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const data = new FormData(form);
  const indices = String(data.get("indices") || "")
    .split(/[\s,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const payload = {
    start_date: data.get("start_date"),
    end_date: data.get("end_date"),
    sync_daily: data.has("sync_daily"),
    sync_financials: data.has("sync_financials"),
    sync_indices: data.has("sync_indices"),
    indices,
    max_standard_financial_symbols: Number(data.get("financial_limit") || 100),
  };
  button.disabled = true;
  button.textContent = "正在创建任务";
  try {
    const run = await api("/api/data/tushare/sync", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast(`同步任务 ${run.run_id} 已创建`);
    await refreshDataView();
    pollSyncRun(run.run_id);
  } catch (error) {
    showToast(error.message, "error");
    button.disabled = false;
    button.textContent = "开始同步";
  }
}

async function refreshDataView() {
  const [status, indices] = await Promise.all([
    api("/api/data/status"),
    api("/api/data/indices"),
  ]);
  state.dataStatus = status;
  state.indices = indices.items;
  if (state.view === "data") renderData();
}

async function pollSyncRun(runId) {
  try {
    const run = await api(`/api/data/sync-runs/${runId}`);
    await refreshDataView();
    if (run.status === "queued" || run.status === "running") {
      window.setTimeout(() => pollSyncRun(runId), 2000);
      return;
    }
    await loadCoreData();
    if (state.view === "data") renderData();
    showToast(
      run.status === "completed" ? "真实数据同步完成" : run.error_message || "同步失败",
      run.status === "completed" ? "info" : "error",
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderFactors() {
  workspace.innerHTML = `
    <section class="panel">
      <div class="panel-header">
        <div><h2>因子目录</h2><p>评估采用 RankIC、ICIR、分组多空收益和样本覆盖</p></div>
        <span class="badge">POINT-IN-TIME</span>
      </div>
      <div class="form-grid compact-form">
        <div class="field"><label for="factor-forward-days">预测周期（交易日）</label><input id="factor-forward-days" type="number" value="5" min="1" max="60" /></div>
        <div class="field"><label for="factor-neutralize">行业市值中性化</label><select id="factor-neutralize"><option value="false">否</option><option value="true">是</option></select></div>
      </div>
      <div class="table-shell">
        <table>
          <thead><tr><th>因子</th><th>分类</th><th>表达式</th><th>版本</th><th>IC</th><th>ICIR</th><th>多空年化</th><th>操作</th></tr></thead>
          <tbody>
            ${state.factors
              .map((factor) => {
                const metrics = factor.latest_metrics || {};
                return `<tr>
                  <td><strong>${escapeHtml(factor.name)}</strong><div class="subtle mono">${escapeHtml(factor.factor_id)}</div></td>
                  <td>${escapeHtml(factor.category)}</td>
                  <td class="mono">${escapeHtml(factor.expression)}</td>
                  <td>${escapeHtml(factor.version)}</td>
                  <td>${number(metrics.ic_mean, 4)}</td>
                  <td>${number(metrics.ic_ir)}</td>
                  <td class="${metrics.annualized_long_short_return >= 0 ? "positive" : "negative"}">${percent(metrics.annualized_long_short_return)}</td>
                  <td><button class="button small secondary factor-evaluate" data-factor="${escapeHtml(factor.factor_id)}">重新评估</button></td>
                </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </div>
    </section>`;
  document.querySelectorAll(".factor-evaluate").forEach((button) => {
    button.addEventListener("click", () => evaluateFactor(button));
  });
}

async function evaluateFactor(button) {
  button.disabled = true;
  button.textContent = "计算中";
  try {
    const forwardDays = Number(document.querySelector("#factor-forward-days").value);
    const neutralize = document.querySelector("#factor-neutralize").value === "true";
    const task = await api(`/api/factors/${button.dataset.factor}/evaluate`, {
      method: "POST",
      body: JSON.stringify({
        forward_days: forwardDays,
        neutralize,
      }),
    });
    const result = await waitForTask(task.task_id, "因子评估中");
    showToast(`评估完成，IC ${number(result.metrics.ic_mean, 4)}`);
    await loadCoreData();
    renderFactors();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function factorOptions(selected = "momentum_20") {
  return state.factors
    .map(
      (factor) =>
        `<option value="${escapeHtml(factor.factor_id)}" ${factor.factor_id === selected ? "selected" : ""}>${escapeHtml(factor.name)}</option>`,
    )
    .join("");
}

function renderBacktest() {
  const latest = state.backtests[0];
  workspace.innerHTML = `
    <section class="panel">
      <div class="panel-header">
        <div><h2>运行策略回测</h2><p>A股手数、佣金、卖出印花税与滑点已启用</p></div>
        <span class="badge">EVENT SIMULATION</span>
      </div>
      <form id="backtest-form">
        <div class="form-grid">
          <div class="field span-2"><label for="bt-name">实验名称</label><input id="bt-name" name="name" value="多因子选股回测" /></div>
          <div class="field"><label for="bt-factor">选股因子</label><select id="bt-factor" name="factor_id">${factorOptions()}</select></div>
          <div class="field"><label for="bt-capital">初始资金</label><input id="bt-capital" name="initial_capital" type="number" value="1000000" min="10000" /></div>
          <div class="field"><label for="bt-top">持仓数量</label><input id="bt-top" name="top_n" type="number" value="3" min="1" max="20" /></div>
          <div class="field"><label for="bt-rebalance">调仓周期（交易日）</label><input id="bt-rebalance" name="rebalance_days" type="number" value="5" min="1" max="60" /></div>
          <div class="field"><label for="bt-universe">股票池</label><select id="bt-universe" name="universe"><option value="">全市场</option><option value="000300.SH">沪深300</option><option value="000905.SH">中证500</option><option value="000852.SH">中证1000</option><option value="000300.SH,000905.SH">沪深300+中证500</option></select></div>
          <div class="field"><label for="bt-buffer">持仓缓冲带</label><select id="bt-buffer" name="buffer_multiple"><option value="1">关闭</option><option value="1.5">1.5x</option><option value="2">2.0x</option><option value="3">3.0x</option></select></div>
          <div class="field"><label for="bt-exclude-st">剔除ST</label><select id="bt-exclude-st" name="exclude_st"><option value="false">否</option><option value="true">是</option></select></div>
          <div class="field"><label for="bt-price-limit">涨跌停约束</label><select id="bt-price-limit" name="apply_price_limit"><option value="false">否</option><option value="true">是</option></select></div>
          <div class="field"><label for="bt-neutralize">行业市值中性化</label><select id="bt-neutralize" name="neutralize"><option value="false">否</option><option value="true">是</option></select></div>
          <div class="field"><label for="bt-commission">佣金率</label><input id="bt-commission" name="commission_rate" type="number" step="0.0001" value="0.0003" /></div>
          <div class="field"><label for="bt-slippage">滑点（bps）</label><input id="bt-slippage" name="slippage_bps" type="number" value="5" min="0" max="100" /></div>
        </div>
        <div class="form-actions"><button class="button" type="submit">开始回测</button></div>
      </form>
    </section>
    <div id="backtest-result">
      ${latest ? backtestResultMarkup(latest) : '<div class="empty-state">运行第一个策略回测</div>'}
    </div>`;
  document.querySelector("#backtest-form").addEventListener("submit", runBacktest);
  if (latest) loadBacktestChart(latest.run_id, "backtest-chart");
}

function backtestResultMarkup(result) {
  const metrics = result.metrics;
  const hasBenchmark = metrics.alpha != null;
  const excessRow = hasBenchmark
    ? `
    <div class="metric-grid" style="margin-top:16px">
      ${metric("超额收益", percent(metrics.excess_return), `基准 ${percent(metrics.benchmark_return)}`, metrics.excess_return >= 0 ? "positive" : "negative")}
      ${metric("年化Alpha", percent(metrics.alpha), "策略年化-基准年化", metrics.alpha >= 0 ? "positive" : "negative")}
      ${metric("信息比率", number(metrics.information_ratio), "Alpha/跟踪误差")}
      ${metric("超额最大回撤", percent(metrics.excess_max_drawdown), "相对基准", "negative")}
    </div>`
    : "";
  return `
    <div class="metric-grid" style="margin-top:24px">
      ${metric("总收益", percent(metrics.total_return), "成本后", metrics.total_return >= 0 ? "positive" : "negative")}
      ${metric("年化收益", percent(metrics.annualized_return), "252交易日年化", metrics.annualized_return >= 0 ? "positive" : "negative")}
      ${metric("夏普比率", number(metrics.sharpe_ratio), "无风险利率 2%")}
      ${metric("最大回撤", percent(metrics.max_drawdown), "峰值到谷值", "negative")}
    </div>
    ${excessRow}
    <section class="panel" style="margin-top:24px">
      <div class="panel-header">
        <div><h2>${escapeHtml(result.name)}</h2><p>${escapeHtml(result.run_id)}</p></div>
        <span class="badge">${escapeHtml(result.factor_id)}</span>
      </div>
      <div class="chart-wrap"><canvas id="backtest-chart"></canvas></div>
    </section>`;
}

async function runBacktest(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const data = new FormData(event.currentTarget);
  const universeValue = data.get("universe");
  const payload = {
    name: data.get("name"),
    factor_id: data.get("factor_id"),
    initial_capital: Number(data.get("initial_capital")),
    top_n: Number(data.get("top_n")),
    rebalance_days: Number(data.get("rebalance_days")),
    commission_rate: Number(data.get("commission_rate")),
    stamp_duty_rate: 0.0005,
    slippage_bps: Number(data.get("slippage_bps")),
    universe_indices: universeValue ? universeValue.split(",") : null,
    buffer_multiple: Number(data.get("buffer_multiple")),
    exclude_st: data.get("exclude_st") === "true",
    apply_price_limit: data.get("apply_price_limit") === "true",
    neutralize: data.get("neutralize") === "true",
  };
  button.disabled = true;
  button.textContent = "正在回测";
  try {
    const task = await api("/api/backtests", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const completed = await waitForTask(task.task_id, "策略回测中");
    const result = await api(`/api/backtests/${completed.run_id}`);
    state.activeBacktest = result;
    document.querySelector("#backtest-result").innerHTML = backtestResultMarkup(result);
    drawLineChart(document.querySelector("#backtest-chart"), result.equity_curve, "equity", "benchmark");
    showToast(`回测完成，生成 ${result.trades.length} 笔成交`);
    await loadCoreData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "开始回测";
  }
}

function renderRisk() {
  workspace.innerHTML = `
    <div class="workspace-grid">
      <section class="panel">
        <div class="panel-header">
          <div><h2>组合预交易检查</h2><p>模拟买单复用同一套仓位、敞口、回撤和单笔规则</p></div>
          <span class="badge">PRE-TRADE</span>
        </div>
        <form id="risk-form">
          <div class="form-grid">
            <div class="field span-2"><label for="risk-symbol-1">标的 1</label><select id="risk-symbol-1">${instrumentOptions(0)}</select></div>
            <div class="field span-2"><label for="risk-weight-1">目标权重</label><input id="risk-weight-1" type="number" step="0.01" value="0.22" min="0" max="1" /></div>
            <div class="field span-2"><label for="risk-symbol-2">标的 2</label><select id="risk-symbol-2">${instrumentOptions(1)}</select></div>
            <div class="field span-2"><label for="risk-weight-2">目标权重</label><input id="risk-weight-2" type="number" step="0.01" value="0.22" min="0" max="1" /></div>
            <div class="field"><label for="risk-turnover">计划换手率</label><input id="risk-turnover" type="number" step="0.01" value="0.35" /></div>
            <div class="field"><label for="risk-drawdown">当前回撤</label><input id="risk-drawdown" type="number" step="0.01" value="0.08" /></div>
            <div class="field span-2"><label for="risk-order">最大单笔委托 / 净值</label><input id="risk-order" type="number" step="0.01" value="0.12" /></div>
          </div>
          <div class="form-actions"><button class="button" type="submit">执行风险检查</button></div>
        </form>
        <div id="risk-result"></div>
      </section>
      <aside class="panel">
        <div class="panel-header"><div><h2>当前硬规则</h2><p>不可被 Agent 修改</p></div></div>
        <div class="status-list">
          <div class="status-row"><span>单标的上限</span><strong>25%</strong></div>
          <div class="status-row"><span>总敞口上限</span><strong>100%</strong></div>
          <div class="status-row"><span>日换手率上限</span><strong>50%</strong></div>
          <div class="status-row"><span>组合熔断回撤</span><strong>15%</strong></div>
          <div class="status-row"><span>单笔委托上限</span><strong>20%</strong></div>
        </div>
      </aside>
    </div>`;
  document.querySelector("#risk-form").addEventListener("submit", runRiskCheck);
}

function instrumentOptions(selectedIndex) {
  return state.instruments
    .map(
      (item, index) =>
        `<option value="${escapeHtml(item.symbol)}" ${index === selectedIndex ? "selected" : ""}>${escapeHtml(item.symbol)} ${escapeHtml(item.name)}</option>`,
    )
    .join("");
}

async function runRiskCheck(event) {
  event.preventDefault();
  const payload = {
    positions: [
      { symbol: document.querySelector("#risk-symbol-1").value, weight: Number(document.querySelector("#risk-weight-1").value) },
      { symbol: document.querySelector("#risk-symbol-2").value, weight: Number(document.querySelector("#risk-weight-2").value) },
    ],
    proposed_turnover: Number(document.querySelector("#risk-turnover").value),
    current_drawdown: Number(document.querySelector("#risk-drawdown").value),
    order_notional_weight: Number(document.querySelector("#risk-order").value),
  };
  try {
    const result = await api("/api/risk/check", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const target = document.querySelector("#risk-result");
    target.innerHTML = `
      <div class="result-strip ${result.approved ? "" : "rejected"}">
        <strong>${result.approved ? "APPROVED：通过预交易检查" : "REJECTED：订单已阻断"}</strong>
        ${result.breaches.length ? `<ul>${result.breaches.map((item) => `<li>${escapeHtml(item.message)}</li>`).join("")}</ul>` : "<p>未发现仓位、敞口、换手、回撤或委托规模违规。</p>"}
      </div>`;
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderAgent() {
  workspace.innerHTML = `
    <div class="workspace-grid">
      <section class="panel">
        <div class="panel-header">
          <div><h2>只读研究 Agent</h2><p>可读取因子、回测和风险，不具备订单与资金权限</p></div>
          <span class="badge">READ ONLY</span>
        </div>
        <form id="agent-form">
          <div class="form-grid">
            <div class="field full"><label for="agent-question">研究问题</label><textarea id="agent-question">分析当前动量因子的有效性、回撤风险和进入模拟盘前仍需完成的验证。</textarea></div>
            <div class="field span-2"><label for="agent-factor">关联因子</label><select id="agent-factor"><option value="">不指定</option>${factorOptions()}</select></div>
            <div class="field span-2"><label for="agent-backtest">关联回测</label><select id="agent-backtest"><option value="">使用最近实验</option>${state.backtests.map((item) => `<option value="${escapeHtml(item.run_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.factor_id)}</option>`).join("")}</select></div>
          </div>
          <div class="form-actions"><button class="button" type="submit">开始分析</button></div>
        </form>
      </section>
      <aside class="panel">
        <div class="panel-header"><div><h2>权限边界</h2><p>代码级隔离</p></div></div>
        <div class="status-list">
          <div class="status-row"><span>读取因子</span><span class="badge">ALLOW</span></div>
          <div class="status-row"><span>读取回测</span><span class="badge">ALLOW</span></div>
          <div class="status-row"><span>提交订单</span><span class="badge danger">DENY</span></div>
          <div class="status-row"><span>修改资金</span><span class="badge danger">DENY</span></div>
        </div>
      </aside>
    </div>
    <section class="panel">
      <div class="panel-header"><div><h2>研究输出</h2><p id="agent-mode">等待任务</p></div></div>
      <div id="agent-output" class="agent-output">研究结果会显示在这里。</div>
      <div id="agent-evidence" class="evidence-list" style="margin-top:12px"></div>
    </section>`;
  document.querySelector("#agent-form").addEventListener("submit", runAgent);
}

function renderPaper() {
  const account = state.paperAccount;
  workspace.innerHTML = `
    <div class="metric-grid">
      ${metric("账户净值", `¥${number(account.equity)}`, account.name)}
      ${metric("可用现金", `¥${number(account.available_cash)}`, `冻结 ¥${number(account.reserved_cash)}`)}
      ${metric("持仓市值", `¥${number(account.market_value)}`, `${account.positions.length} 个持仓`)}
      ${metric("当日盈亏", `¥${number(account.daily_pnl)}`, `限额 ¥${number(account.daily_loss_limit)}`, account.daily_pnl >= 0 ? "positive" : "negative")}
    </div>
    <div class="workspace-grid">
      <section class="panel">
        <div class="panel-header">
          <div><h2>提交模拟委托</h2><p>市价单即时模拟成交，未触及的限价单保持挂起</p></div>
          <span class="badge">PAPER ONLY</span>
        </div>
        <form id="paper-order-form">
          <div class="form-grid">
            <div class="field span-2"><label for="paper-symbol">标的</label><select id="paper-symbol">${instrumentOptions(0)}</select></div>
            <div class="field"><label for="paper-side">方向</label><select id="paper-side"><option value="buy">买入</option><option value="sell">卖出</option></select></div>
            <div class="field"><label for="paper-quantity">数量</label><input id="paper-quantity" type="number" value="100" step="100" min="100" /></div>
            <div class="field"><label for="paper-order-type">委托类型</label><select id="paper-order-type"><option value="market">市价</option><option value="limit">限价</option></select></div>
            <div class="field"><label for="paper-limit-price">限价</label><input id="paper-limit-price" type="number" min="0.01" step="0.01" disabled /></div>
          </div>
          <div class="form-actions"><button class="button" type="submit">提交模拟委托</button></div>
        </form>
        <div id="paper-order-result"></div>
      </section>
      <aside class="panel">
        <div class="panel-header"><div><h2>执行边界</h2><p>当前版本</p></div></div>
        <div class="status-list">
          <div class="status-row"><span>100股整数手</span><span class="badge">ON</span></div>
          <div class="status-row"><span>单笔净值上限 20%</span><span class="badge">ON</span></div>
          <div class="status-row"><span>佣金与印花税</span><span class="badge">ON</span></div>
          <div class="status-row"><span>实盘路由</span><span class="badge danger">OFF</span></div>
        </div>
      </aside>
    </div>
    <section class="panel">
      <div class="panel-header"><div><h2>当前持仓</h2><p>按最新研究快照估值</p></div></div>
      ${paperPositionsTable(account.positions)}
    </section>
    <section class="panel">
      <div class="panel-header"><div><h2>最近委托</h2><p>client_order_id 保证幂等</p></div></div>
      ${paperOrdersTable(account.orders)}
    </section>`;
  document.querySelector("#paper-order-form").addEventListener("submit", submitPaperOrder);
  document.querySelectorAll("[data-cancel-order]").forEach((button) => {
    button.addEventListener("click", () => cancelPaperOrder(button.dataset.cancelOrder));
  });
  document.querySelector("#paper-order-type").addEventListener("change", (event) => {
    const limitInput = document.querySelector("#paper-limit-price");
    limitInput.disabled = event.currentTarget.value !== "limit";
    limitInput.required = event.currentTarget.value === "limit";
    if (limitInput.disabled) limitInput.value = "";
  });
}

function paperPositionsTable(items) {
  if (!items.length) return '<div class="empty-state">当前没有模拟持仓</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>标的</th><th>数量</th><th>可卖</th><th>平均成本</th><th>最新价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td><strong>${escapeHtml(item.name)}</strong><div class="subtle mono">${escapeHtml(item.symbol)}</div></td>
      <td>${item.quantity}</td><td>${item.sellable_quantity}</td><td>${number(item.average_cost, 4)}</td><td>${number(item.last_price, 4)}</td>
      <td>¥${number(item.market_value)}</td><td class="${item.unrealized_pnl >= 0 ? "positive" : "negative"}">¥${number(item.unrealized_pnl)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function paperOrdersTable(items) {
  if (!items.length) return '<div class="empty-state">暂无模拟委托</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>委托ID</th><th>标的</th><th>方向</th><th>数量</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td class="mono">${escapeHtml(item.client_order_id)}</td><td>${escapeHtml(item.symbol)}</td>
      <td>${item.side === "buy" ? "买入" : "卖出"}</td><td>${item.quantity}</td>
      <td><span class="badge ${item.status === "REJECTED" ? "danger" : ""}">${escapeHtml(item.status)}</span></td>
      <td>${escapeHtml(item.created_at.slice(0, 16).replace("T", " "))}</td>
      <td>${["OPEN", "PARTIALLY_FILLED", "UNKNOWN"].includes(item.status) ? `<button class="button secondary" data-cancel-order="${escapeHtml(item.order_id)}">撤单</button>` : "—"}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

async function cancelPaperOrder(orderId) {
  try {
    const result = await api(`/api/paper/orders/${encodeURIComponent(orderId)}/cancel`, {
      method: "POST",
    });
    state.paperAccount = result.account;
    renderPaper();
    showToast(`订单状态：${result.order.status}`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderStrategy() {
  const guard = state.liveGuard;
  const clock = state.marketClock;
  const keyConfigured = Boolean(window.localStorage.getItem("quantdevAdminKey"));
  const readKeyConfigured = Boolean(window.localStorage.getItem("quantdevReadKey"));
  const operator = window.localStorage.getItem("quantdevOperator") || "local-admin";
  workspace.innerHTML = `
    <div class="metric-grid">
      ${metric("交易时钟", clock.can_trade ? "可交易" : "关闭", clock.session, clock.can_trade ? "positive" : "negative")}
      ${metric("Broker 模式", guard.broker_mode, guard.live_trading_enabled ? "实盘总开关开启" : "实盘总开关关闭")}
      ${metric("Kill Switch", guard.kill_switch.active ? "ACTIVE" : "OFF", guard.kill_switch.reason || "运行正常", guard.kill_switch.active ? "negative" : "positive")}
      ${metric("当前阶段", state.readiness.current_stage, state.readiness.all_ready ? "全部就绪" : "仍有前置检查")}
    </div>
    <div class="workspace-grid">
      <section class="panel">
        <div class="panel-header"><div><h2>策略配置</h2><p>因子信号生成目标仓位与幂等订单意图</p></div></div>
        <form id="strategy-form">
          <div class="form-grid">
            <div class="field span-2"><label for="strategy-name">名称</label><input id="strategy-name" value="多因子模拟实盘" required /></div>
            <div class="field span-2"><label for="strategy-factor">因子</label><select id="strategy-factor">${factorOptions()}</select></div>
            <div class="field"><label for="strategy-topn">持仓数</label><input id="strategy-topn" type="number" value="5" min="1" max="20" /></div>
            <div class="field"><label for="strategy-cycle">调仓周期</label><input id="strategy-cycle" type="number" value="5" min="1" max="60" /></div>
            <div class="field"><label for="strategy-notional">单票目标金额</label><input id="strategy-notional" type="number" value="5000" min="100" step="100" /></div>
            <div class="field"><label for="strategy-enabled">创建后状态</label><select id="strategy-enabled"><option value="false">停用</option><option value="true">启用</option></select></div>
          </div>
          <div class="form-actions"><button class="button" type="submit">创建策略</button></div>
        </form>
      </section>
      <aside class="panel">
        <div class="panel-header"><div><h2>操作认证</h2><p>只读、管理、操作员身份分离</p></div></div>
        <div class="field"><label for="read-key">只读密钥</label><input id="read-key" type="password" value="" placeholder="${readKeyConfigured ? "已保存在本机浏览器" : "未配置"}" /></div>
        <div class="field"><label for="admin-key">管理员/操作员密钥</label><input id="admin-key" type="password" value="" placeholder="${keyConfigured ? "已保存在本机浏览器" : "未配置"}" /></div>
        <div class="field"><label for="operator-name">操作员名</label><input id="operator-name" value="${escapeHtml(operator)}" /></div>
        <div class="form-actions">
          <button class="button secondary" id="save-admin-key" type="button">保存认证</button>
          <button class="button ${guard.kill_switch.active ? "" : "danger"}" id="toggle-kill" type="button">${guard.kill_switch.active ? "解除熔断" : "立即熔断"}</button>
        </div>
      </aside>
    </div>
    <section class="panel">
      <div class="panel-header"><div><h2>已配置策略</h2><p>自动运行前先保持停用并手动验证</p></div></div>
      ${strategyTable(state.strategies)}
    </section>
    <section class="panel">
      <div class="panel-header"><div><h2>最近运行</h2><p>信号日期、运行状态与失败原因</p></div></div>
      ${strategyRunsTable(state.strategyRuns)}
    </section>
    <section class="panel" id="strategy-intent-panel">
      <div class="panel-header"><div><h2>订单意图</h2><p>选择一次运行查看审批与执行状态</p></div></div>
      ${state.activeStrategyRun ? strategyIntentTable(state.activeStrategyRun) : '<div class="empty-state">尚未选择策略运行</div>'}
    </section>
    <section class="panel">
      <div class="panel-header"><div><h2>实盘就绪度</h2><p>按阶段逐项放行</p></div></div>
      <div class="status-list">${state.readiness.stages.map((stage) => `
        <div class="status-row"><span>${escapeHtml(stage.title)}</span><span class="badge ${stage.ready ? "" : "danger"}">${stage.ready ? "READY" : "BLOCKED"}</span></div>
      `).join("")}</div>
    </section>`;
  document.querySelector("#strategy-form").addEventListener("submit", createStrategy);
  document.querySelector("#save-admin-key").addEventListener("click", saveAdminKey);
  document.querySelector("#toggle-kill").addEventListener("click", toggleKillSwitch);
  document.querySelectorAll("[data-run-strategy]").forEach((button) => {
    button.addEventListener("click", () => runStrategy(button.dataset.runStrategy));
  });
  document.querySelectorAll("[data-enable-strategy]").forEach((button) => {
    button.addEventListener("click", () => setStrategyEnabled(
      button.dataset.enableStrategy,
      button.dataset.enabled !== "true",
    ));
  });
  bindStrategyRunActions();
}

function strategyTable(items) {
  if (!items.length) return '<div class="empty-state">暂无策略配置</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>名称</th><th>因子</th><th>持仓数</th><th>周期</th><th>单票金额</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td><strong>${escapeHtml(item.name)}</strong><div class="subtle mono">${escapeHtml(item.strategy_id)}</div></td>
      <td>${escapeHtml(item.factor_id)}</td><td>${item.top_n}</td><td>${item.rebalance_days} 日</td><td>¥${number(item.max_order_notional)}</td>
      <td><span class="badge ${item.enabled ? "" : "danger"}">${item.enabled ? "ENABLED" : "DISABLED"}</span></td>
      <td><button class="button secondary" data-run-strategy="${escapeHtml(item.strategy_id)}">运行</button> <button class="button secondary" data-enable-strategy="${escapeHtml(item.strategy_id)}" data-enabled="${item.enabled}">${item.enabled ? "停用" : "启用"}</button></td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function strategyRunsTable(items) {
  if (!items.length) return '<div class="empty-state">暂无策略运行记录</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>运行ID</th><th>策略</th><th>交易日</th><th>信号日</th><th>状态</th><th>错误</th><th>操作</th></tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td class="mono">${escapeHtml(item.run_id)}</td><td class="mono">${escapeHtml(item.strategy_id)}</td>
      <td>${escapeHtml(item.trade_date)}</td><td>${escapeHtml(item.signal_date || "—")}</td>
      <td><span class="badge ${["FAILED", "PARTIAL"].includes(item.status) ? "danger" : ""}">${escapeHtml(item.status)}</span></td>
      <td>${escapeHtml(item.error_message || "—")}</td>
      <td><button class="button secondary" data-view-run="${escapeHtml(item.run_id)}">查看意图</button></td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function strategyIntentTable(run) {
  if (!run.intents.length) return '<div class="empty-state">本次运行无需调仓</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>模式</th><th>审批</th><th>状态</th><th>订单号</th><th>操作</th></tr></thead>
    <tbody>${run.intents.map((item) => `<tr>
      <td class="mono">${escapeHtml(item.symbol)}</td>
      <td>${item.side === "buy" ? "买入" : "卖出"}</td>
      <td>${item.quantity}</td>
      <td>${escapeHtml(item.execution_mode)}</td>
      <td>${escapeHtml(item.approval_status)}</td>
      <td><span class="badge ${["REJECTED", "BLOCKED", "ERROR"].includes(item.status) ? "danger" : ""}">${escapeHtml(item.status)}</span></td>
      <td class="mono">${escapeHtml(item.broker_order_id || item.paper_order_id || "—")}</td>
      <td>${intentActions(item)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function intentActions(item) {
  if (item.execution_mode !== "live") return "—";
  const actions = [];
  if (["PENDING_APPROVAL", "BLOCKED"].includes(item.status)) {
    actions.push(`<button class="button secondary" data-approve-intent="${escapeHtml(item.intent_id)}">审批</button>`);
  }
  if (item.approval_status === "APPROVED" && ["PENDING_APPROVAL", "APPROVED", "BLOCKED"].includes(item.status)) {
    actions.push(`<button class="button" data-submit-intent="${escapeHtml(item.intent_id)}">提交</button>`);
  }
  if (!["FILLED", "CANCELLED", "REJECTED", "DRY_RUN"].includes(item.status)) {
    actions.push(`<button class="button danger" data-reject-intent="${escapeHtml(item.intent_id)}">拒绝</button>`);
  }
  if (item.live_order_id && ["OPEN", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"].includes(item.status)) {
    actions.push(`<button class="button secondary" data-cancel-live="${escapeHtml(item.live_order_id)}">撤单</button>`);
  }
  return actions.join(" ") || "—";
}

function bindStrategyRunActions() {
  document.querySelectorAll("[data-view-run]").forEach((button) => {
    button.addEventListener("click", () => loadStrategyRun(button.dataset.viewRun));
  });
  document.querySelectorAll("[data-approve-intent]").forEach((button) => {
    button.addEventListener("click", () => approveIntent(button.dataset.approveIntent));
  });
  document.querySelectorAll("[data-submit-intent]").forEach((button) => {
    button.addEventListener("click", () => submitIntent(button.dataset.submitIntent));
  });
  document.querySelectorAll("[data-reject-intent]").forEach((button) => {
    button.addEventListener("click", () => rejectIntent(button.dataset.rejectIntent));
  });
  document.querySelectorAll("[data-cancel-live]").forEach((button) => {
    button.addEventListener("click", () => cancelLiveOrder(button.dataset.cancelLive));
  });
}

async function loadStrategyRun(runId) {
  try {
    state.activeStrategyRun = await api(`/api/strategies/runs/${encodeURIComponent(runId)}`);
    renderStrategy();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function refreshActiveStrategyRun() {
  if (!state.activeStrategyRun) return;
  state.activeStrategyRun = await api(`/api/strategies/runs/${encodeURIComponent(state.activeStrategyRun.run_id)}`);
}

async function approveIntent(intentId) {
  try {
    await api(`/api/strategies/intents/${encodeURIComponent(intentId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ reason: "前端人工核对通过" }),
    });
    await Promise.all([refreshStrategyData(), refreshActiveStrategyRun()]);
    renderStrategy();
    showToast("订单意图已审批，请在有效期内提交");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function submitIntent(intentId) {
  try {
    const result = await api(`/api/strategies/intents/${encodeURIComponent(intentId)}/submit`, {
      method: "POST",
    });
    await Promise.all([refreshStrategyData(), refreshActiveStrategyRun()]);
    renderStrategy();
    showToast(`券商订单状态：${result.order.status}`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function rejectIntent(intentId) {
  if (!window.confirm("确认拒绝该订单意图？")) return;
  try {
    await api(`/api/strategies/intents/${encodeURIComponent(intentId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: "前端人工拒绝" }),
    });
    await Promise.all([refreshStrategyData(), refreshActiveStrategyRun()]);
    renderStrategy();
    showToast("订单意图已拒绝");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function cancelLiveOrder(orderId) {
  try {
    const result = await api(`/api/live/orders/${encodeURIComponent(orderId)}/cancel`, {
      method: "POST",
    });
    await Promise.all([refreshStrategyData(), refreshActiveStrategyRun()]);
    renderStrategy();
    showToast(`撤单状态：${result.order.status}`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function refreshStrategyData() {
  const [guard, readiness, strategies, runs, clock] = await Promise.all([
    api("/api/live/guard"),
    api("/api/live/readiness"),
    api("/api/strategies"),
    api("/api/strategies/runs"),
    api("/api/live/market-clock"),
  ]);
  state.liveGuard = guard;
  state.readiness = readiness;
  state.strategies = strategies.items;
  state.strategyRuns = runs.items;
  state.marketClock = clock;
}

function saveAdminKey() {
  const readValue = document.querySelector("#read-key").value.trim();
  const adminValue = document.querySelector("#admin-key").value.trim();
  const operator = document.querySelector("#operator-name").value.trim() || "local-admin";
  if (readValue) window.localStorage.setItem("quantdevReadKey", readValue);
  else window.localStorage.removeItem("quantdevReadKey");
  if (adminValue) window.localStorage.setItem("quantdevAdminKey", adminValue);
  else window.localStorage.removeItem("quantdevAdminKey");
  window.localStorage.setItem("quantdevOperator", operator);
  showToast("认证信息已保存在本机浏览器");
  renderStrategy();
}

async function createStrategy(event) {
  event.preventDefault();
  try {
    await api("/api/strategies", {
      method: "POST",
      body: JSON.stringify({
        name: document.querySelector("#strategy-name").value,
        factor_id: document.querySelector("#strategy-factor").value,
        top_n: Number(document.querySelector("#strategy-topn").value),
        rebalance_days: Number(document.querySelector("#strategy-cycle").value),
        max_order_notional: Number(document.querySelector("#strategy-notional").value),
        enabled: document.querySelector("#strategy-enabled").value === "true",
      }),
    });
    await refreshStrategyData();
    renderStrategy();
    showToast("策略已创建");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function runStrategy(strategyId) {
  try {
    const result = await api("/api/strategies/run", {
      method: "POST",
      body: JSON.stringify({ strategy_id: strategyId, force: false }),
    });
    await loadCoreData();
    renderStrategy();
    showToast(`策略运行状态：${result.status}`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function setStrategyEnabled(strategyId, enabled) {
  try {
    await api(`/api/strategies/${encodeURIComponent(strategyId)}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    await refreshStrategyData();
    renderStrategy();
    showToast(enabled ? "策略已启用" : "策略已停用");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function toggleKillSwitch() {
  const active = !state.liveGuard.kill_switch.active;
  try {
    await api("/api/live/kill-switch", {
      method: "POST",
      body: JSON.stringify({
        active,
        reason: active ? "前端手动熔断" : "前端人工确认后解除",
      }),
    });
    await refreshStrategyData();
    renderStrategy();
    showToast(active ? "Kill Switch 已激活" : "Kill Switch 已解除");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function submitPaperOrder(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  button.textContent = "提交中";
  const clientOrderId = `web-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const orderType = document.querySelector("#paper-order-type").value;
  const limitPrice = document.querySelector("#paper-limit-price").value;
  try {
    const result = await api("/api/paper/orders", {
      method: "POST",
      body: JSON.stringify({
        client_order_id: clientOrderId,
        symbol: document.querySelector("#paper-symbol").value,
        side: document.querySelector("#paper-side").value,
        quantity: Number(document.querySelector("#paper-quantity").value),
        order_type: orderType,
        limit_price: orderType === "limit" ? Number(limitPrice) : null,
      }),
    });
    state.paperAccount = result.account;
    const message =
      result.status === "FILLED"
        ? `成交完成，价格 ${number(result.fill_price, 4)}`
        : result.status === "OPEN"
          ? "限价未触及，委托已挂起"
          : result.reject_reason;
    showToast(message, result.status === "REJECTED" ? "error" : "info");
    renderPaper();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "提交模拟委托";
  }
}

async function runAgent(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  button.textContent = "分析中";
  try {
    const result = await api("/api/agent/research", {
      method: "POST",
      body: JSON.stringify({
        question: document.querySelector("#agent-question").value,
        factor_id: document.querySelector("#agent-factor").value || null,
        backtest_run_id: document.querySelector("#agent-backtest").value || null,
      }),
    });
    document.querySelector("#agent-mode").textContent = result.mode;
    document.querySelector("#agent-output").textContent = result.answer;
    document.querySelector("#agent-evidence").innerHTML = result.evidence
      .map((item) => `<div class="evidence-item">${escapeHtml(JSON.stringify(item))}</div>`)
      .join("");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "开始分析";
  }
}

const EXPLORER_TABS = [
  ["prices", "个股行情"],
  ["financials", "财务指标"],
  ["factor", "因子打分榜"],
  ["screener", "市场筛选"],
];

function fmtCell(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return number(value);
  return String(value);
}

function renderExplorer() {
  const ex = state.explorer;
  const tabBar = EXPLORER_TABS.map(
    ([key, label]) =>
      `<button class="button ${ex.tab === key ? "" : "secondary"}" data-ex-tab="${key}">${label}</button>`,
  ).join(" ");
  let body = "";
  if (ex.tab === "prices") body = explorerPricesTab(ex);
  else if (ex.tab === "financials") body = explorerFinancialsTab(ex);
  else if (ex.tab === "factor") body = explorerFactorTab(ex);
  else body = explorerScreenerTab(ex);
  workspace.innerHTML = `
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">${tabBar}</div>
    ${body}`;
  wireExplorer(ex);
}

function explorerPricesTab(ex) {
  const p = ex.prices;
  let detail = `<div class="empty-state compact">输入股票代码后点击查询</div>`;
  if (p && p.items && p.items.length) {
    const rows = p.items
      .slice()
      .reverse()
      .map(
        (r) => `<tr>
          <td class="mono">${escapeHtml(r.trade_date)}</td>
          <td>${number(r.open)}</td><td>${number(r.high)}</td>
          <td>${number(r.low)}</td><td><strong>${number(r.close)}</strong></td>
          <td>${Number(r.volume || 0).toLocaleString("zh-CN")}</td>
        </tr>`,
      )
      .join("");
    detail = `
      <div class="panel-header" style="margin-top:8px"><div><h2 style="font-size:16px">${escapeHtml(p.symbol)} 价格走势（前复权）</h2></div></div>
      <div class="chart-wrap"><canvas id="explorer-price-chart"></canvas></div>
      <div class="table-shell"><table>
        <thead><tr><th>交易日</th><th>开</th><th>高</th><th>低</th><th>收</th><th>成交量</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
  }
  return `<section class="panel">
    <div class="panel-header"><div><h2>个股行情</h2><p>输入股票代码查看历史价格与走势</p></div></div>
    <div class="form-grid">
      <div class="field span-2"><label for="ex-symbol">股票代码</label>
        <input id="ex-symbol" value="${escapeHtml(ex.symbol)}" placeholder="如 600519.SH / 000001.SZ" /></div>
      <div class="form-actions" style="align-self:end"><button class="button" id="ex-price-query">查询</button></div>
    </div>
    ${detail}
  </section>`;
}

function explorerFinancialsTab(ex) {
  const f = ex.financials;
  let table = `<div class="empty-state compact">输入股票代码后点击查询</div>`;
  if (f) {
    if (!f.items || !f.items.length) {
      table = `<div class="empty-state compact">该标的暂无财务数据</div>`;
    } else {
      const cols = Object.keys(f.items[0]);
      table = `<div class="table-shell"><table>
        <thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
        <tbody>${f.items
          .map(
            (row) =>
              `<tr>${cols.map((c) => `<td class="mono">${escapeHtml(fmtCell(row[c]))}</td>`).join("")}</tr>`,
          )
          .join("")}</tbody></table></div>`;
    }
  }
  return `<section class="panel">
    <div class="panel-header"><div><h2>财务指标</h2><p>输入股票代码查看财务指标历史（左右可滚动）</p></div></div>
    <div class="form-grid">
      <div class="field span-2"><label for="ex-fin-symbol">股票代码</label>
        <input id="ex-fin-symbol" value="${escapeHtml(ex.symbol)}" placeholder="如 600519.SH" /></div>
      <div class="form-actions" style="align-self:end"><button class="button" id="ex-fin-query">查询</button></div>
    </div>
    ${table}
  </section>`;
}

function explorerFactorTab(ex) {
  const options = state.factors
    .map(
      (item) =>
        `<option value="${escapeHtml(item.factor_id)}" ${ex.factorId === item.factor_id ? "selected" : ""}>${escapeHtml(item.name || item.factor_id)}</option>`,
    )
    .join("");
  const s = ex.factorScores;
  const board = (items, title) => `<section class="panel">
    <div class="panel-header"><div><h2 style="font-size:16px">${title}</h2></div></div>
    <div class="table-shell"><table>
      <thead><tr><th>#</th><th>代码</th><th>名称</th><th>行业</th><th>因子值</th></tr></thead>
      <tbody>${items
        .map(
          (it, i) =>
            `<tr><td>${i + 1}</td><td class="mono">${escapeHtml(it.symbol)}</td><td>${escapeHtml(it.name)}</td><td>${escapeHtml(it.industry)}</td><td><strong>${number(it.score, 4)}</strong></td></tr>`,
        )
        .join("")}</tbody></table></div></section>`;
  return `<section class="panel">
    <div class="panel-header"><div><h2>因子打分榜</h2><p>读取最新缓存；缓存缺失时使用管理员权限创建后台刷新任务</p></div></div>
    <div class="form-grid">
      <div class="field span-2"><label for="ex-factor">因子</label><select id="ex-factor">${options}</select></div>
      <div class="form-actions" style="align-self:end"><button class="button" id="ex-factor-query">查询</button></div>
    </div>
  </section>
  ${
    s
      ? `<div class="subtle" style="margin:4px 0 12px">信号日 ${escapeHtml(s.signal_date || "")} · 全市场 ${s.count} 只</div>
         ${board(s.top, "📈 得分最高 Top")} ${board(s.bottom, "📉 得分最低 Bottom")}`
      : `<div class="empty-state compact">选择因子后点击查询缓存</div>`
  }`;
}

function explorerScreenerTab(ex) {
  const industries = Array.from(
    new Set(state.instruments.map((i) => i.industry).filter(Boolean)),
  ).sort();
  let list = state.instruments.slice();
  if (ex.screenIndustry) list = list.filter((i) => i.industry === ex.screenIndustry);
  if (ex.screenKeyword.trim()) {
    const kw = ex.screenKeyword.trim();
    list = list.filter((i) => `${i.symbol}${i.name || ""}`.includes(kw));
  }
  if (ex.screenSort === "name")
    list.sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh"));
  else if (ex.screenSort === "industry")
    list.sort((a, b) => (a.industry || "").localeCompare(b.industry || "", "zh"));
  else list.sort((a, b) => a.symbol.localeCompare(b.symbol));
  const shown = list.slice(0, 200);
  const opts =
    `<option value="">全部行业</option>` +
    industries
      .map(
        (i) =>
          `<option value="${escapeHtml(i)}" ${ex.screenIndustry === i ? "selected" : ""}>${escapeHtml(i)}</option>`,
      )
      .join("");
  return `<section class="panel">
    <div class="panel-header"><div><h2>市场筛选</h2><p>共 ${state.instruments.length} 只，按筛选后展示前 ${shown.length}（关键词输入后按回车）</p></div></div>
    <div class="form-grid">
      <div class="field"><label for="ex-screen-industry">行业</label><select id="ex-screen-industry">${opts}</select></div>
      <div class="field"><label for="ex-screen-kw">代码/名称关键词</label><input id="ex-screen-kw" value="${escapeHtml(ex.screenKeyword)}" placeholder="如 银行 / 600" /></div>
      <div class="field"><label for="ex-screen-sort">排序</label><select id="ex-screen-sort">
        <option value="symbol" ${ex.screenSort === "symbol" ? "selected" : ""}>按代码</option>
        <option value="name" ${ex.screenSort === "name" ? "selected" : ""}>按名称</option>
        <option value="industry" ${ex.screenSort === "industry" ? "selected" : ""}>按行业</option>
      </select></div>
    </div>
    <div class="table-shell"><table>
      <thead><tr><th>代码</th><th>名称</th><th>交易所</th><th>行业</th><th></th></tr></thead>
      <tbody>${shown
        .map(
          (i) => `<tr>
          <td class="mono">${escapeHtml(i.symbol)}</td><td>${escapeHtml(i.name)}</td>
          <td>${escapeHtml(i.exchange)}</td><td>${escapeHtml(i.industry)}</td>
          <td><button class="button secondary" data-ex-view-symbol="${escapeHtml(i.symbol)}">看行情</button></td>
        </tr>`,
        )
        .join("")}</tbody></table></div>
  </section>`;
}

function wireExplorer(ex) {
  document.querySelectorAll("[data-ex-tab]").forEach((b) =>
    b.addEventListener("click", () => {
      ex.tab = b.dataset.exTab;
      renderExplorer();
    }),
  );
  const bind = (id, event, fn) => {
    const el = document.querySelector(id);
    if (el) el.addEventListener(event, fn);
  };
  const onEnter = (id, fn) =>
    bind(id, "keydown", (e) => {
      if (e.key === "Enter") fn();
    });
  bind("#ex-price-query", "click", explorerQueryPrices);
  onEnter("#ex-symbol", explorerQueryPrices);
  bind("#ex-fin-query", "click", explorerQueryFinancials);
  onEnter("#ex-fin-symbol", explorerQueryFinancials);
  bind("#ex-factor-query", "click", explorerQueryFactor);
  bind("#ex-screen-industry", "change", (e) => {
    ex.screenIndustry = e.target.value;
    renderExplorer();
  });
  bind("#ex-screen-sort", "change", (e) => {
    ex.screenSort = e.target.value;
    renderExplorer();
  });
  bind("#ex-screen-kw", "input", (e) => {
    ex.screenKeyword = e.target.value;
  });
  onEnter("#ex-screen-kw", renderExplorer);
  document.querySelectorAll("[data-ex-view-symbol]").forEach((b) =>
    b.addEventListener("click", () => {
      ex.symbol = b.dataset.exViewSymbol;
      ex.tab = "prices";
      explorerQueryPrices();
    }),
  );
  if (ex.tab === "prices" && ex.prices && ex.prices.items && ex.prices.items.length) {
    const canvas = document.querySelector("#explorer-price-chart");
    if (canvas)
      drawLineChart(
        canvas,
        ex.prices.items.map((r) => ({ date: r.trade_date, close: Number(r.close) })),
        "close",
      );
  }
}

async function explorerQueryPrices() {
  const ex = state.explorer;
  const input = document.querySelector("#ex-symbol");
  const symbol = (input ? input.value : ex.symbol).trim();
  if (!symbol) return;
  ex.symbol = symbol;
  try {
    ex.prices = await api(`/api/market/prices?symbol=${encodeURIComponent(symbol)}&limit=120`);
    ex.tab = "prices";
    renderExplorer();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function explorerQueryFinancials() {
  const ex = state.explorer;
  const input = document.querySelector("#ex-fin-symbol");
  const symbol = (input ? input.value : ex.symbol).trim();
  if (!symbol) return;
  ex.symbol = symbol;
  try {
    ex.financials = await api(
      `/api/data/financials/${encodeURIComponent(symbol)}?limit=20`,
    );
    renderExplorer();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function explorerQueryFactor() {
  const ex = state.explorer;
  const select = document.querySelector("#ex-factor");
  const factorId = select ? select.value : ex.factorId;
  if (!factorId) {
    showToast("请选择一个因子", "error");
    return;
  }
  ex.factorId = factorId;
  showToast("正在读取因子分数缓存");
  try {
    ex.factorScores = await api(
      `/api/factors/${encodeURIComponent(factorId)}/scores?limit=30`,
    );
    renderExplorer();
  } catch (error) {
    const adminKey = window.localStorage.getItem("quantdevAdminKey") || "";
    if (!adminKey) {
      showToast(error.message, "error");
      return;
    }
    try {
      const task = await api(
        `/api/factors/${encodeURIComponent(factorId)}/scores/refresh`,
        { method: "POST" },
      );
      await waitForTask(task.task_id, "因子分数缓存刷新中");
      ex.factorScores = await api(
        `/api/factors/${encodeURIComponent(factorId)}/scores?limit=30`,
      );
      renderExplorer();
      showToast("因子分数缓存已刷新");
    } catch (refreshError) {
      showToast(refreshError.message, "error");
    }
  }
}

function render() {
  const [eyebrow, title] = viewMeta[state.view];
  document.querySelector("#page-eyebrow").textContent = eyebrow;
  document.querySelector("#page-title").textContent = title;
  if (!state.dashboard) {
    workspace.innerHTML = '<div class="empty-state">正在初始化研究环境...</div>';
    return;
  }
  const renderers = {
    overview: renderOverview,
    data: renderData,
    explorer: renderExplorer,
    factors: renderFactors,
    backtest: renderBacktest,
    risk: renderRisk,
    paper: renderPaper,
    strategy: renderStrategy,
    agent: renderAgent,
  };
  renderers[state.view]();
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    state.view = button.dataset.view;
    render();
  });
});

document.querySelector("#refresh-button").addEventListener("click", async () => {
  try {
    await loadCoreData();
    render();
    showToast("数据已刷新");
  } catch (error) {
    showToast(error.message, "error");
  }
});

window.addEventListener("resize", () => {
  if (state.view === "overview" && state.activeBacktest) {
    const canvas = document.querySelector("#overview-chart");
    if (canvas) drawLineChart(canvas, state.activeBacktest.equity_curve, "equity", "benchmark");
  }
});

try {
  await loadCoreData();
  render();
} catch (error) {
  workspace.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  showToast(error.message, "error");
}
