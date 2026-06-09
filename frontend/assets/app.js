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
  activeBacktest: null,
};

const viewMeta = {
  overview: ["WORKSPACE / OVERVIEW", "量化研究总览"],
  data: ["RESEARCH / DATA", "数据中心"],
  factors: ["RESEARCH / FACTORS", "因子实验室"],
  backtest: ["RESEARCH / BACKTEST", "策略回测"],
  risk: ["PORTFOLIO / RISK", "风险检查"],
  paper: ["EXECUTION / PAPER", "模拟交易"],
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
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "请求失败");
  }
  return payload;
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
  ] = await Promise.all([
    api("/api/dashboard"),
    api("/api/factors"),
    api("/api/backtests"),
    api("/api/instruments"),
    api("/api/system/requirements"),
    api("/api/paper/account"),
    api("/api/data/status"),
    api("/api/data/indices"),
  ]);
  state.dashboard = dashboard;
  state.factors = factors.items;
  state.backtests = backtests.items;
  state.instruments = instruments.items;
  state.requirements = requirements;
  state.paperAccount = paperAccount;
  state.dataStatus = dataStatus;
  state.indices = indices.items;
  document.querySelector("#snapshot-label").textContent =
    dashboard.snapshot_id || "暂无数据快照";
}

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
    const result = await api(`/api/factors/${button.dataset.factor}/evaluate`, {
      method: "POST",
      body: JSON.stringify({
        forward_days: forwardDays,
        neutralize,
      }),
    });
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
    const result = await api("/api/backtests", {
      method: "POST",
      body: JSON.stringify(payload),
    });
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
      ${metric("可用现金", `¥${number(account.cash)}`, "即时成交模拟")}
      ${metric("持仓市值", `¥${number(account.market_value)}`, `${account.positions.length} 个持仓`)}
      ${metric("累计收益", percent(account.total_return), "初始资金 ¥1,000,000", account.total_return >= 0 ? "positive" : "negative")}
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
    <thead><tr><th>标的</th><th>数量</th><th>平均成本</th><th>最新价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td><strong>${escapeHtml(item.name)}</strong><div class="subtle mono">${escapeHtml(item.symbol)}</div></td>
      <td>${item.quantity}</td><td>${number(item.average_cost, 4)}</td><td>${number(item.last_price, 4)}</td>
      <td>¥${number(item.market_value)}</td><td class="${item.unrealized_pnl >= 0 ? "positive" : "negative"}">¥${number(item.unrealized_pnl)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function paperOrdersTable(items) {
  if (!items.length) return '<div class="empty-state">暂无模拟委托</div>';
  return `<div class="table-shell"><table>
    <thead><tr><th>委托ID</th><th>标的</th><th>方向</th><th>数量</th><th>状态</th><th>时间</th></tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td class="mono">${escapeHtml(item.client_order_id)}</td><td>${escapeHtml(item.symbol)}</td>
      <td>${item.side === "buy" ? "买入" : "卖出"}</td><td>${item.quantity}</td>
      <td><span class="badge ${item.status === "REJECTED" ? "danger" : ""}">${escapeHtml(item.status)}</span></td>
      <td>${escapeHtml(item.created_at.slice(0, 16).replace("T", " "))}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
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

function render() {
  const [eyebrow, title] = viewMeta[state.view];
  document.querySelector("#page-eyebrow").textContent = eyebrow;
  document.querySelector("#page-title").textContent = title;
  const renderers = {
    overview: renderOverview,
    data: renderData,
    factors: renderFactors,
    backtest: renderBacktest,
    risk: renderRisk,
    paper: renderPaper,
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
