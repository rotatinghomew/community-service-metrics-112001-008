"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  bootstrap: null,
  scenario: {},
  result: null,
  comparison: new Map(),
  pending: 0,
  requestSeq: 0,
  lastError: "",
};

const PRESETS = {
  base: {
    utilization: 80, utilization_spread: 10, backlog_target_pct: 50,
    calendar_mode: "apply", calendar_uncertainty: 25, weekly_trend_pct: 0,
  },
  conservative: {
    utilization: 70, utilization_spread: 8, backlog_target_pct: 35,
    calendar_mode: "apply", calendar_uncertainty: 35, weekly_trend_pct: -3,
  },
  stress: {
    utilization: 90, utilization_spread: 10, backlog_target_pct: 70,
    calendar_mode: "apply", calendar_uncertainty: 40, weekly_trend_pct: 8,
  },
  "no-calendar": {
    calendar_mode: "ignore", calendar_uncertainty: 0,
  },
};

const STATUS_META = {
  covered: { label: "可覆盖", badge: "badge-covered", row: "" },
  at_risk: { label: "临界", badge: "badge-risk", row: "" },
  partial: { label: "缺口", badge: "badge-partial", row: "" },
  unfillable: { label: "无人可派", badge: "badge-unfillable", row: "row-unfillable" },
  unpredictable: { label: "不可预测", badge: "badge-unknown", row: "row-unknown" },
  insufficient: { label: "资料不足", badge: "badge-unknown", row: "row-unknown" },
  low_confidence: { label: "低置信度", badge: "badge-unknown", row: "" },
};

const TEXT = {
  covered: "可覆盖", at_risk: "临界", partial: "缺口", unfillable: "无人可派",
  unpredictable: "不可预测", insufficient: "资料不足", low_confidence: "低置信度",
  cancel: "撤销", transfer: "转派", supplement: "补录",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function num(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits).replace(/\.0+$/, "");
}

function fmtHours(value) {
  if (value === null || value === undefined) return "—";
  return `${num(value, 1)}<span class="unit">h</span>`;
}

function fmtRange(range, digits = 1) {
  if (!range) return "—";
  return `<span class="range"><span class="bounds">${num(range.low, digits)} – </span>` +
    `<span class="typical">${num(range.typical, digits)}</span>` +
    `<span class="bounds"> – ${num(range.high, digits)}</span></span>`;
}

function fmtRangeText(range, digits = 1) {
  if (!range) return "—";
  return `${num(range.low, digits)} – ${num(range.typical, digits)} – ${num(range.high, digits)}`;
}

function weekShort(week) {
  const match = /^(\d{4})-W(\d{2})$/.exec(week || "");
  return match ? `W${match[2]}` : (week || "—");
}

function showError(message) {
  const banner = $("#error-banner");
  if (!message) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = { error: `服务器返回了非 JSON 响应（${response.status}）` };
  }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（${response.status}）`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

/* ---------------- scenario form ---------------- */

function readForm() {
  const form = $("#scenario-form");
  const data = new FormData(form);
  const minutes = {};
  $$("#service-minutes-body .minute-row").forEach((row) => {
    const category = row.dataset.category;
    const low = Number(row.querySelector("[data-part='low']").value);
    const typical = Number(row.querySelector("[data-part='typical']").value);
    const high = Number(row.querySelector("[data-part='high']").value);
    minutes[category] = { low, typical, high };
  });
  return {
    start_week: data.get("start_week") || "",
    horizon_weeks: Number(data.get("horizon_weeks")),
    history_weeks: Number(data.get("history_weeks")),
    weekly_trend_pct: Number(data.get("weekly_trend_pct")) / 100,
    late_entry_mode: data.get("late_entry_mode"),
    utilization: Number(data.get("utilization")) / 100,
    utilization_spread: Number(data.get("utilization_spread")) / 100,
    backlog_target_pct: Number(data.get("backlog_target_pct")) / 100,
    include_paused_backlog: form.querySelector("[name='include_paused_backlog']").checked,
    calendar_mode: data.get("calendar_mode"),
    calendar_uncertainty: Number(data.get("calendar_uncertainty")) / 100,
    service_minutes: minutes,
    custom_factors: state.scenario.custom_factors || [],
  };
}

function writeForm(scenario) {
  const form = $("#scenario-form");
  form.querySelector("[name='start_week']").value = scenario.start_week || "";
  form.querySelector("[name='horizon_weeks']").value = scenario.horizon_weeks ?? 2;
  form.querySelector("[name='history_weeks']").value = scenario.history_weeks ?? 4;
  form.querySelector("[name='weekly_trend_pct']").value = Math.round((scenario.weekly_trend_pct ?? 0) * 100);
  form.querySelector("[name='late_entry_mode']").value = scenario.late_entry_mode || "range";
  form.querySelector("[name='utilization']").value = Math.round((scenario.utilization ?? 0.8) * 100);
  form.querySelector("[name='utilization_spread']").value = Math.round((scenario.utilization_spread ?? 0.1) * 100);
  form.querySelector("[name='backlog_target_pct']").value = Math.round((scenario.backlog_target_pct ?? 0.5) * 100);
  form.querySelector("[name='calendar_mode']").value = scenario.calendar_mode || "apply";
  form.querySelector("[name='calendar_uncertainty']").value = Math.round((scenario.calendar_uncertainty ?? 0.25) * 100);
  form.querySelector("[name='include_paused_backlog']").checked = Boolean(scenario.include_paused_backlog);
}

function renderServiceMinutes(categories) {
  const body = $("#service-minutes-body");
  const defaults = state.bootstrap.default_service_minutes || {};
  body.innerHTML = categories.map((category) => {
    const values = (state.scenario.service_minutes && state.scenario.service_minutes[category]) ||
      defaults[category] || { low: 20, typical: 45, high: 90 };
    const assumed = !defaults[category];
    return `<div class="minute-row" data-category="${esc(category)}">
      <span>${esc(category)}${assumed ? ' <em title="该类别没有专门假设，使用通用区间">*</em>' : ""}</span>
      <div><label>低</label><input type="number" min="1" max="1440" step="5" data-part="low" value="${num(values.low, 0)}" aria-label="${esc(category)} 低值分钟"></div>
      <div><label>典型</label><input type="number" min="1" max="1440" step="5" data-part="typical" value="${num(values.typical, 0)}" aria-label="${esc(category)} 典型分钟"></div>
      <div><label>高</label><input type="number" min="1" max="1440" step="5" data-part="high" value="${num(values.high, 0)}" aria-label="${esc(category)} 高值分钟"></div>
    </div>`;
  }).join("");
}

function setScenario(next, { redrawMinutes = false } = {}) {
  state.scenario = { ...state.scenario, ...next };
  writeForm(state.scenario);
  if (redrawMinutes) renderServiceMinutes(state.bootstrap.categories);
  scheduleAnalyse(120);
}

/* ---------------- analysis ---------------- */

let analyseTimer = null;
function scheduleAnalyse(delay = 260) {
  clearTimeout(analyseTimer);
  analyseTimer = setTimeout(runAnalyse, delay);
}

async function runAnalyse() {
  const seq = ++state.requestSeq;
  const scenario = readForm();
  state.scenario = scenario;
  const main = $(".main");
  main.classList.add("loading");
  try {
    const result = await api("/api/analyse", {
      method: "POST",
      body: JSON.stringify({ scenario }),
    });
    if (seq !== state.requestSeq) return;
    state.result = result;
    showError("");
    renderAll(result);
  } catch (error) {
    if (seq !== state.requestSeq) return;
    const prefix = error.payload && error.payload.kind === "data" ? "资料不一致：" : "参数有误：";
    showError(prefix + error.message);
  } finally {
    if (seq === state.requestSeq) main.classList.remove("loading");
  }
}

/* ---------------- rendering ---------------- */

function renderAll(result) {
  renderTopbar(result);
  renderKpis(result);
  renderPlanBanner(result);
  renderAssumptions(result);
  renderForecastTable(result);
  renderSchedule(result);
  renderSideTables(result);
  renderBacklog(result);
  renderPlanDiff(result);
  renderWarnings(result);
  renderAdjustmentList();
  renderSavedScenarios();
}

function renderTopbar(result) {
  const meta = $("#topbar-meta");
  const weeks = result.meta.weeks;
  const util = result.meta.utilization;
  meta.innerHTML = [
    `<span class="pill">起始 ${esc(result.meta.start_week)}</span>`,
    `<span class="pill">预测 ${weeks.length} 周</span>`,
    `<span class="pill">历史 ${esc(result.meta.history_weeks.join("、") || "无")}</span>`,
    `<span class="pill">可兑现率 ${num(util.typical * 100, 0)}% (${num(util.low * 100, 0)}–${num(util.high * 100, 0)}%)</span>`,
  ].join("");
}

function renderKpis(result) {
  const summary = result.summary;
  $("#kpi-demand").innerHTML = fmtHours(summary.demand_hours.typical);
  $("#kpi-demand-note").textContent =
    `区间 ${fmtRangeText(summary.demand_hours)} 小时；仅含可预测单元`;

  $("#kpi-capacity").innerHTML = fmtHours(summary.deliverable_hours.typical);
  $("#kpi-capacity-note").textContent =
    `区间 ${fmtRangeText(summary.deliverable_hours)} 小时；已扣除休息与跨点重叠`;

  $("#kpi-gap").innerHTML = fmtHours(summary.gap_hours.typical);
  $("#kpi-gap-note").textContent =
    `最坏情形 ${num(summary.gap_hours.high, 1)} 小时；按技能分列，不互相抵消`;

  $("#kpi-unfillable").innerHTML = fmtHours(summary.unfillable_demand_hours.typical);
  $("#kpi-unfillable-note").textContent =
    `上沿 ${num(summary.unfillable_demand_hours.high, 1)} 小时；无有效资质人员可派`;

  $("#kpi-backlog").innerHTML = fmtHours(summary.backlog_hours);
  $("#kpi-backlog-note").textContent =
    `可纳入消化 ${num(summary.backlog_eligible_hours, 1)} 小时；独立于新到件`;
}

function renderPlanBanner(result) {
  const banner = $("#plan-banner");
  const plan = result.confirmed_plan;
  if (!plan) {
    banner.hidden = true;
    return;
  }
  const diff = result.plan_diff;
  banner.hidden = false;
  const sameInputs = plan.input_fingerprint === result.meta.input_fingerprint;
  if (diff && diff.has_changes) {
    banner.innerHTML = `<strong>已确认班表「${esc(plan.label)}」未被修改。</strong>` +
      `当前资料或情景会产生 ${diff.added.length + diff.removed.length + diff.changed.length} 处差异，` +
      `请查看下方「已确认班表与当前建议」后决定是否重新确认。` +
      (sameInputs ? "" : "（输入指纹已变化）");
  } else if (!sameInputs) {
    banner.innerHTML = `<strong>已确认班表「${esc(plan.label)}」未被修改。</strong>` +
      "输入指纹与当前分析不同，但分配时数没有变化；请核对假设是否影响结论。";
  } else {
    banner.innerHTML = `<strong>已确认班表「${esc(plan.label)}」与当前建议一致。</strong>` +
      "当前输入未发生变化。";
  }
}

function renderAssumptions(result) {
  const body = $("#assumptions-body");
  const rows = result.meta.assumptions || [];
  if (!rows.length) {
    body.innerHTML = '<p class="list-empty">没有记录假设。</p>';
    return;
  }
  body.innerHTML = `<ul class="assumption-list">${
    rows.map((item) => `<li>
      <div class="assumption-topic">
        <span class="source-tag source-${esc(item.source)}">${
          item.source === "assumption" ? "假设" : item.source === "derived" ? "推算" : "口径"
        }</span>
        ${esc(item.topic)}
      </div>
      <p>${esc(item.detail)}</p>
    </li>`).join("")
  }</ul>`;
}

function statusBadge(status) {
  const meta = STATUS_META[status] || STATUS_META.unpredictable;
  return `<span class="badge ${meta.badge}">${esc(meta.label)}</span>`;
}

function renderForecastTable(result) {
  const tbody = $("#forecast-table tbody");
  const coverage = new Map(
    result.coverage.map((item) => [`${item.week}|${item.grid}|${item.category}`, item])
  );
  const rows = result.forecast.map((forecast) => {
    const cover = coverage.get(`${forecast.week}|${forecast.grid}|${forecast.category}`) || {};
    const meta = STATUS_META[cover.status] || STATUS_META.unpredictable;
    const isUnknown = cover.status === "unpredictable" || cover.status === "insufficient";
    let demandCell = "—";
    let capacityCell = "—";
    let gapCell = "—";
    if (isUnknown) {
      demandCell = `<span class="muted">${cover.status === "insufficient" ? "资料不足" : "不可预测"}</span>`;
      capacityCell = cover.reference_capacity_hours
        ? `<span class="range"><span class="bounds">参考上限 </span><span class="typical">${num(cover.reference_capacity_hours, 1)}</span><span class="bounds">h</span></span>`
        : "—";
      gapCell = `<span class="muted">不计入合计</span>`;
    } else {
      demandCell = fmtRange(forecast.demand_hours);
      capacityCell = fmtRange(cover.capacity_hours);
      gapCell = fmtRange(cover.gap_hours);
    }
    const notes = [];
    if (forecast.assumed_minutes) notes.push("工时使用通用假设");
    if (forecast.factors && forecast.factors.length) {
      notes.push("系数 " + forecast.factors.map((factor) =>
        `${esc(factor.event)} ×${num(factor.factor, 2)}`).join("、"));
    }
    if (forecast.confidence === "low" && !isUnknown) notes.push("历史样本少");
    if (cover.status === "unfillable") notes.push("无该技能可兑现人员");
    if (cover.message && (isUnknown || cover.status === "unfillable")) notes.push(cover.message);
    return `<tr class="${meta.row}">
      <td class="num">${esc(weekShort(forecast.week))}<span class="muted"> · ${esc(forecast.week.slice(0, 4))}</span></td>
      <td>${esc(forecast.grid)}</td>
      <td>${esc(forecast.category)}${forecast.demand_status === "low_confidence" ? ' <span class="badge badge-unknown">低置信</span>' : ""}</td>
      <td>${demandCell}</td>
      <td>${capacityCell}</td>
      <td>${gapCell}</td>
      <td>${statusBadge(cover.status || forecast.status)}</td>
      <td class="note-cell">${notes.length ? notes.map(esc).join("；") : "—"}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.length
    ? rows.join("")
    : '<tr class="empty-row"><td colspan="8">没有可展示的行。</td></tr>';
}

function renderSchedule(result) {
  const tbody = $("#schedule-table tbody");
  const rows = result.schedule.assignments;
  tbody.innerHTML = rows.length
    ? rows.map((item) => `<tr>
        <td class="num">${esc(weekShort(item.week))}</td>
        <td>${esc(item.grid)}</td>
        <td>${esc(item.skill)}</td>
        <td>${esc(item.person)}</td>
        <td class="num">${num(item.hours, 1)}</td>
        <td class="num">${esc((item.shift_ids || []).join("、") || "—")}</td>
        <td class="note-cell">${esc(item.basis)}</td>
      </tr>`).join("")
    : '<tr class="empty-row"><td colspan="7">当前预测周没有已排班次，或没有可分配的技能需求。</td></tr>';
}

function renderSideTables(result) {
  const additions = result.schedule.additional_shifts;
  $("#additional-shifts").innerHTML = additions.length
    ? `<table class="mini-table">
        <thead><tr><th>周</th><th>网格</th><th>技能</th><th>缺口（低–典型–高）</th><th>参考班次</th></tr></thead>
        <tbody>${additions.map((item) => `<tr class="${item.unfillable ? "unfillable-row" : ""}">
          <td>${esc(weekShort(item.week))}</td>
          <td>${esc(item.grid)}</td>
          <td>${esc(item.skill)}</td>
          <td class="num">${fmtRangeText(item.gap_hours)}</td>
          <td class="num">${item.shifts_needed.low}–${item.shifts_needed.typical}–${item.shifts_needed.high} 班${
            item.unfillable ? ' <span class="badge badge-unfillable">无人可派</span>' : ""
          }</td>
        </tr>`).join("")}</tbody>
      </table>
      <p class="hint">${esc(additions[0].note)}</p>`
    : '<p class="list-empty">按当前点估计没有缺口。</p>';

  const idle = result.schedule.idle_capacity;
  $("#idle-capacity").innerHTML = idle.length
    ? `<table class="mini-table">
        <thead><tr><th>周</th><th>网格</th><th>人员</th><th>小时</th><th>原因</th></tr></thead>
        <tbody>${idle.map((item) => `<tr>
          <td>${esc(weekShort(item.week))}</td>
          <td>${esc(item.grid)}</td>
          <td>${esc(item.person)}</td>
          <td class="num">${num(item.hours, 1)}</td>
          <td class="note-cell">${esc(item.reason)}</td>
        </tr>`).join("")}</tbody>
      </table>
      <p class="hint">空闲时数不等于可以跨技能抵扣；它只能用于该人员具备资质的同类需求。</p>`
    : '<p class="list-empty">没有未分配的班次时数。</p>';
}

function renderBacklog(result) {
  const items = result.backlog.items;
  $("#backlog-table tbody").innerHTML = items.length
    ? items.map((item) => `<tr>
        <td class="num">${esc(item.case)}</td>
        <td>${esc(item.grid)}</td>
        <td>${esc(item.category)}</td>
        <td class="num">${num(item.estimated_hours, 1)}</td>
        <td>${esc(item.status)}</td>
        <td class="note-cell">${esc(item.reason)}</td>
      </tr>`).join("")
    : '<tr class="empty-row"><td colspan="6">没有未结事项记录。</td></tr>';

  const targets = result.backlog.targets;
  $("#backlog-target-table tbody").innerHTML = targets.length
    ? targets.map((item) => `<tr>
        <td class="num">${esc(weekShort(item.week))}</td>
        <td>${esc(item.grid)}</td>
        <td>${esc(item.category)}</td>
        <td class="num">${num(item.target_hours, 1)}</td>
        <td class="num">${num(item.capacity_after_new_demand_hours, 1)}</td>
        <td class="num">${num(item.coverable_hours, 1)}</td>
        <td class="num">${item.shortfall_hours > 0 ? `<strong>${num(item.shortfall_hours, 1)}</strong>` : "0"}</td>
      </tr>`).join("")
    : '<tr class="empty-row"><td colspan="7">没有可纳入消化目标的未结积压。</td></tr>';
}

function renderPlanDiff(result) {
  const container = $("#plan-diff");
  const plan = result.confirmed_plan;
  $("#confirmed-meta").textContent = plan
    ? `确认于 ${plan.confirmed_at || "未知时间"} · ${plan.weeks.length} 周`
    : "尚未确认任何班表";
  $("#clear-plan").disabled = !plan;
  if (!plan) {
    container.innerHTML = '<p class="list-empty">确认后会在此比较「已确认」与「当前建议」，资料或修订变化不会自动改写已确认班表。</p>';
    return;
  }
  const diff = result.plan_diff;
  if (!diff || !diff.has_changes) {
    container.innerHTML = `<p class="diff-ok">当前建议与已确认班表「${esc(plan.label)}」一致（${diff ? diff.unchanged_count : 0} 条分配相同）。</p>`;
    return;
  }
  const block = (title, items, klass, render) => items.length
    ? `<div class="diff-block ${klass}"><h3>${title}（${items.length}）</h3><ul>${
        items.map(render).join("")
      }</ul></div>`
    : "";
  container.innerHTML = `<div class="diff-grid">
    ${block("新增建议", diff.added, "diff-added", (item) =>
      `<li>${esc(weekShort(item.week))} · ${esc(item.grid)} · ${esc(item.skill)} · ${esc(item.person)} +${num(item.hours)}h</li>`)}
    ${block("建议移除", diff.removed, "diff-removed", (item) =>
      `<li>${esc(weekShort(item.week))} · ${esc(item.grid)} · ${esc(item.skill)} · ${esc(item.person)} −${num(item.hours)}h</li>`)}
    ${block("时数变化", diff.changed, "diff-changed", (item) =>
      `<li>${esc(weekShort(item.week))} · ${esc(item.grid)} · ${esc(item.skill)} · ${esc(item.person)}：${num(item.confirmed_hours)}h → ${num(item.recommended_hours)}h</li>`)}
  </div>`;
}

function renderWarnings(result) {
  const list = $("#warning-list");
  const warnings = result.meta.warnings || [];
  const unique = warnings.filter((item) => !item.includes("的需求不可预测，未按 0 计入合计"));
  const unpredictable = warnings.length - unique.length;
  const rows = unique.map((item) => `<li><span class="item-main">${esc(item)}</span></li>`);
  if (unpredictable > 0) {
    rows.push(`<li><span class="item-main">另有 ${unpredictable} 个「网格/类别/周」单元因资料不足不可预测；这些单元未按零需求计入合计。</span></li>`);
  }
  list.innerHTML = rows.length ? rows.join("") : '<li><span class="item-main">没有额外提示。</span></li>';
}

/* ---------------- adjustments ---------------- */

function renderAdjustmentList() {
  const list = $("#adjustment-list");
  const items = (state.bootstrap.state && state.bootstrap.state.adjustments) || [];
  if (!items.length) {
    list.innerHTML = '<li class="list-empty"><span>没有运行时修订。</span></li>';
    return;
  }
  list.innerHTML = items.map((item) => {
    const delta = item.count_delta > 0 ? `+${item.count_delta}` : `${item.count_delta}`;
    const target = item.kind === "transfer" ? ` → ${esc(item.to_grid)}` : "";
    return `<li>
      <div class="item-main">
        <div class="item-title">${esc(TEXT[item.kind] || item.kind)} ${esc(item.grid)}${target} · ${esc(item.category)} · ${esc(item.week)}</div>
        <div class="item-sub">${delta} 件${item.note ? ` · ${esc(item.note)}` : ""}</div>
      </div>
      <button type="button" class="btn btn-small btn-ghost" data-delete-adjustment="${esc(item.id)}">删除</button>
    </li>`;
  }).join("");
}

async function refreshBootstrap() {
  state.bootstrap = await api("/api/bootstrap");
  state.scenario = { ...state.bootstrapDefaults() };
  writeForm(state.scenario);
  renderServiceMinutes(state.bootstrap.categories);
  populateSelects();
  renderAdjustmentList();
  renderSavedScenarios();
}

function bootstrapDefaults() {
  return {
    start_week: state.bootstrap.start_week_default,
    horizon_weeks: 2,
    history_weeks: 4,
    late_entry_mode: "range",
    weekly_trend_pct: 0,
    service_minutes: {},
    utilization: 0.8,
    utilization_spread: 0.1,
    backlog_target_pct: 0.5,
    include_paused_backlog: false,
    calendar_mode: "apply",
    calendar_uncertainty: 0.25,
    custom_factors: [],
    label: "",
  };
}

function populateSelects() {
  const boot = state.bootstrap;
  const startSelect = $("#start-week");
  startSelect.innerHTML = boot.start_week_options
    .map((week) => `<option value="${esc(week)}">${esc(week)}</option>`)
    .join("");
  if (!boot.start_week_options.includes(boot.start_week_default)) {
    startSelect.insertAdjacentHTML("afterbegin",
      `<option value="${esc(boot.start_week_default)}">${esc(boot.start_week_default)}</option>`);
  }

  const adjustmentWeeks = Array.from(new Set([...(boot.arrival_weeks || []), ...(boot.shift_weeks || [])]));
  const weekList = $("#adjustment-weeks");
  if (weekList) weekList.innerHTML = adjustmentWeeks.map((week) => `<option value="${esc(week)}"></option>`).join("");

  const gridOptions = boot.grids.map((grid) => `<option value="${esc(grid)}">${esc(grid)}</option>`).join("");
  $("#adj-grid").innerHTML = gridOptions;
  $("#adj-togrid").innerHTML = gridOptions;
  $("#adj-category").innerHTML = boot.categories
    .map((category) => `<option value="${esc(category)}">${esc(category)}</option>`).join("");
  $("#adj-week").setAttribute("list", "adjustment-weeks");
}

/* ---------------- saved scenarios ---------------- */

function renderSavedScenarios() {
  const list = $("#saved-scenarios");
  const saved = (state.bootstrap.state && state.bootstrap.state.scenarios) || [];
  $("#scenario-count").textContent = saved.length ? `${saved.length}/8` : "";
  if (!saved.length) {
    list.innerHTML = '<li class="list-empty"><span>还没有保存的情景。</span></li>';
  } else {
    list.innerHTML = saved.map((item) => `<li>
      <div class="item-main">
        <div class="item-title">${esc(item.label)}</div>
        <div class="item-sub">起始 ${esc(item.scenario.start_week || "自动")} · ${esc(item.scenario.horizon_weeks)} 周 · 可兑现 ${num(item.scenario.utilization * 100, 0)}%</div>
      </div>
      <span class="inline-form">
        <button type="button" class="btn btn-small btn-secondary" data-load-scenario="${esc(item.id)}">加载</button>
        <button type="button" class="btn btn-small btn-ghost" data-delete-scenario="${esc(item.id)}">删除</button>
      </span>
    </li>`).join("");
  }
  renderScenarioCompare();
}

function renderScenarioCompare() {
  const tbody = $("#scenario-compare tbody");
  const saved = (state.bootstrap.state && state.bootstrap.state.scenarios) || [];
  if (!saved.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">保存情景后，这里会列出各自的汇总以便对比。</td></tr>';
    return;
  }
  tbody.innerHTML = saved.map((item) => {
    const result = state.comparison.get(item.id);
    if (!result) {
      return `<tr><td>${esc(item.label)}</td><td colspan="5" class="muted">计算中…</td>
        <td><button type="button" class="btn btn-small btn-ghost" data-delete-scenario="${esc(item.id)}">删除</button></td></tr>`;
    }
    const summary = result.summary;
    return `<tr>
      <td>${esc(item.label)}</td>
      <td class="num">${num(summary.demand_hours.typical)} h</td>
      <td class="num">${fmtRangeText(summary.demand_hours)}</td>
      <td class="num">${num(summary.deliverable_hours.typical)} h</td>
      <td class="num">${num(summary.gap_hours.typical)} h</td>
      <td class="num">${summary.unpredictable_cells}</td>
      <td><button type="button" class="btn btn-small btn-ghost" data-delete-scenario="${esc(item.id)}">删除</button></td>
    </tr>`;
  }).join("");
}

async function refreshComparison() {
  const saved = (state.bootstrap.state && state.bootstrap.state.scenarios) || [];
  await Promise.all(saved.map(async (item) => {
    try {
      const result = await api("/api/analyse", {
        method: "POST",
        body: JSON.stringify({ scenario: item.scenario }),
      });
      state.comparison.set(item.id, result);
    } catch (error) {
      state.comparison.delete(item.id);
    }
  }));
  renderScenarioCompare();
}

/* ---------------- events ---------------- */

function bindEvents() {
  $("#scenario-form").addEventListener("input", (event) => {
    if (event.target.closest("#service-minutes-body")) {
      scheduleAnalyse(320);
    } else {
      scheduleAnalyse(260);
    }
  });
  $("#scenario-form").addEventListener("change", () => scheduleAnalyse(160));

  $("#reset-scenario").addEventListener("click", () => {
    state.scenario = bootstrapDefaults();
    writeForm(state.scenario);
    renderServiceMinutes(state.bootstrap.categories);
    runAnalyse();
  });

  $$("#scenario-form [data-preset]").forEach((button) => {
    button.addEventListener("click", () => {
      const preset = PRESETS[button.dataset.preset];
      if (!preset) return;
      const patch = { ...preset };
      if (patch.utilization !== undefined) patch.utilization = patch.utilization / 100;
      if (patch.utilization_spread !== undefined) patch.utilization_spread = patch.utilization_spread / 100;
      if (patch.backlog_target_pct !== undefined) patch.backlog_target_pct = patch.backlog_target_pct / 100;
      if (patch.calendar_uncertainty !== undefined) patch.calendar_uncertainty = patch.calendar_uncertainty / 100;
      if (patch.weekly_trend_pct !== undefined) patch.weekly_trend_pct = patch.weekly_trend_pct / 100;
      state.scenario = { ...state.scenario, ...patch };
      writeForm(state.scenario);
      scheduleAnalyse(80);
    });
  });

  $("#adj-kind").addEventListener("change", () => {
    const kind = $("#adj-kind").value;
    const field = $("#adj-togrid-field");
    field.hidden = kind !== "transfer";
    const delta = $("#adj-delta");
    if (kind === "supplement") {
      delta.min = "1";
      if (Number(delta.value) < 1) delta.value = "1";
    } else {
      delta.max = "-1";
      if (Number(delta.value) > 0) delta.value = "-1";
    }
  });

  $("#adjustment-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorBox = $("#adjustment-error");
    errorBox.hidden = true;
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {
      kind: data.get("kind"),
      count_delta: Number(data.get("count_delta")),
      grid: data.get("grid"),
      category: data.get("category"),
      week: (data.get("week") || "").trim(),
      to_grid: data.get("kind") === "transfer" ? data.get("to_grid") : null,
      note: (data.get("note") || "").trim(),
    };
    try {
      await api("/api/adjustments", { method: "POST", body: JSON.stringify(payload) });
      form.querySelector("[name='note']").value = "";
      state.bootstrap = await api("/api/bootstrap");
      renderAdjustmentList();
      $("#adj-delta").value = data.get("kind") === "supplement" ? "1" : "-1";
      await runAnalyse();
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = error.message;
    }
  });

  $("#adjustment-list").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-adjustment]");
    if (!button) return;
    button.disabled = true;
    try {
      await api(`/api/adjustments/${encodeURIComponent(button.dataset.deleteAdjustment)}`, { method: "DELETE" });
      state.bootstrap = await api("/api/bootstrap");
      renderAdjustmentList();
      await runAnalyse();
    } catch (error) {
      showError("删除修订失败：" + error.message);
      button.disabled = false;
    }
  });

  $("#save-scenario").addEventListener("click", async () => {
    const label = $("#scenario-label").value.trim();
    const errorBox = $("#scenario-error");
    errorBox.hidden = true;
    if (!label) {
      errorBox.hidden = false;
      errorBox.textContent = "请填写情景名称。";
      $("#scenario-label").focus();
      return;
    }
    try {
      const scenario = readForm();
      await api("/api/scenarios", { method: "POST", body: JSON.stringify({ label, scenario }) });
      $("#scenario-label").value = "";
      state.bootstrap = await api("/api/bootstrap");
      renderSavedScenarios();
      await refreshComparison();
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = error.message;
    }
  });

  $("#saved-scenarios").addEventListener("click", async (event) => {
    const load = event.target.closest("[data-load-scenario]");
    const remove = event.target.closest("[data-delete-scenario]");
    if (load) {
      const saved = state.bootstrap.state.scenarios.find((item) => item.id === load.dataset.loadScenario);
      if (!saved) return;
      state.scenario = { ...saved.scenario };
      writeForm(state.scenario);
      renderServiceMinutes(state.bootstrap.categories);
      runAnalyse();
    }
    if (remove) {
      remove.disabled = true;
      try {
        await api(`/api/scenarios/${encodeURIComponent(remove.dataset.deleteScenario)}`, { method: "DELETE" });
        state.bootstrap = await api("/api/bootstrap");
        state.comparison.delete(remove.dataset.deleteScenario);
        renderSavedScenarios();
      } catch (error) {
        showError("删除情景失败：" + error.message);
        remove.disabled = false;
      }
    }
  });

  $("#scenario-compare").addEventListener("click", async (event) => {
    const remove = event.target.closest("[data-delete-scenario]");
    if (!remove) return;
    remove.disabled = true;
    try {
      await api(`/api/scenarios/${encodeURIComponent(remove.dataset.deleteScenario)}`, { method: "DELETE" });
      state.bootstrap = await api("/api/bootstrap");
      state.comparison.delete(remove.dataset.deleteScenario);
      renderSavedScenarios();
    } catch (error) {
      showError("删除情景失败：" + error.message);
      remove.disabled = false;
    }
  });

  $("#confirm-plan").addEventListener("click", async () => {
    const label = $("#confirm-label").value.trim();
    if (!label) {
      $("#confirm-label").focus();
      showError("请先填写确认班表的名称或版本。");
      return;
    }
    try {
      await api("/api/plan/confirm", {
        method: "POST",
        body: JSON.stringify({
          label,
          confirmed_at: new Date().toISOString(),
          scenario: readForm(),
        }),
      });
      showError("");
      await runAnalyse();
    } catch (error) {
      showError("确认失败：" + error.message);
    }
  });

  $("#clear-plan").addEventListener("click", async () => {
    try {
      await api("/api/plan/confirmed", { method: "DELETE" });
      await runAnalyse();
    } catch (error) {
      showError("清除失败：" + error.message);
    }
  });

  $("#toggle-assumptions").addEventListener("click", (event) => {
    const body = $("#assumptions-body");
    const expanded = event.currentTarget.getAttribute("aria-expanded") === "true";
    event.currentTarget.setAttribute("aria-expanded", String(!expanded));
    event.currentTarget.textContent = expanded ? "展开" : "收起";
    body.hidden = expanded;
  });

  $("#export-json").addEventListener("click", () => {
    if (!state.result) return;
    const blob = new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `负荷分析_${state.result.meta.start_week}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  });
}

/* ---------------- boot ---------------- */

let booted = false;

async function boot() {
  if (booted) return;
  booted = true;
  try {
    state.bootstrap = await api("/api/bootstrap");
    state.scenario = bootstrapDefaults();
    renderServiceMinutes(state.bootstrap.categories);
    populateSelects();
    writeForm(state.scenario);
    renderAdjustmentList();
    renderSavedScenarios();
    bindEvents();
    await runAnalyse();
    await refreshComparison();
  } catch (error) {
    showError("初始化失败：" + error.message);
  }
}

document.addEventListener("DOMContentLoaded", boot);
