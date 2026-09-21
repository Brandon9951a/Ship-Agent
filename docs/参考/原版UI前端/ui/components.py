"""
ui/components.py — HTML 渲染组件（纯渲染，不解析）

职责：接收结构化 dict/list，生成 HTML 字符串
解析逻辑统一在 ui/parsers.py
2026-08-02 整改：Figma 风格 — KPI 卡片 / metro 航线图 / 能耗柱状图 / 状态 pill / 空状态
"""
from typing import List, Dict, Optional
from ui.parsers import (
    parse_route_overview,
    parse_segment_rows,
    parse_speed_rows,
    parse_summary,
    parse_safety,
    parse_recommendations,
    parse_infeasible_detail,
)

# ═══════════════════════════════════════════════════
# 工具图标与中文名映射
# ═══════════════════════════════════════════════════

TOOL_ICONS = {
    "DataAcquisitionTool": "🔍", "DataGetTool": "🔍",
    "SegmentDivisionTool": "🗂️", "DivideSegmentsTool": "🗂️", "RouteSegmentTool": "🗂️",
    "EnergyPredictionModel": "⚡", "EnergyPredictTool": "⚡", "EnergyModel": "⚡",
    "SpeedOptimizationSolver": "🧬", "SpeedOptimizer": "🧬", "DESolver": "🧬",
    "ReportGenerationTool": "📝", "ReportGenerator": "📝",
}

TOOL_NAMES_ZH = {
    "DataAcquisitionTool": "数据获取", "DataGetTool": "数据获取",
    "SegmentDivisionTool": "航段划分", "DivideSegmentsTool": "航段划分", "RouteSegmentTool": "航段划分",
    "EnergyPredictionModel": "能耗预测", "EnergyPredictTool": "能耗预测", "EnergyModel": "能耗预测",
    "SpeedOptimizationSolver": "航速优化", "SpeedOptimizer": "航速优化", "DESolver": "航速优化",
    "ReportGenerationTool": "报告生成", "ReportGenerator": "报告生成",
}

TOOL_ROUND_MAP = {
    "DataAcquisitionTool": 1, "DataGetTool": 1,
    "SegmentDivisionTool": 2, "DivideSegmentsTool": 2, "RouteSegmentTool": 2,
    "EnergyPredictionModel": 3, "EnergyPredictTool": 3, "EnergyModel": 3,
    "SpeedOptimizationSolver": 4, "SpeedOptimizer": 4, "DESolver": 4,
    "ReportGenerationTool": 5, "ReportGenerator": 5,
}

PROGRESS_LABELS = ["数据获取", "航段划分", "能耗预测", "航速优化", "报告生成"]

_EMOJI_RE = "[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]"


def _pick_emoji(text: str) -> str:
    """从标题/文本中提取第一个 emoji，用于空状态图标"""
    import re
    m = re.search(_EMOJI_RE, str(text or ""))
    return m.group(0) if m else "🔄"


# ═══════════════════════════════════════════════════
# 0. 空状态卡片
# ═══════════════════════════════════════════════════

def empty_card(title: str, message: str) -> str:
    icon = _pick_emoji(title)
    return (
        f'<div class="result-card">'
        f'<div class="card-title">{title}</div>'
        f'<div class="empty-inline">'
        f'<span class="empty-icon">{icon}</span>'
        f'<span>{_escape(message)}</span>'
        f'</div>'
        f'</div>'
    )


# ═══════════════════════════════════════════════════
# 1. 进度指示器 + 状态 pill
# ═══════════════════════════════════════════════════

def build_progress_bar(current_round: int, has_error: bool = False) -> str:
    html = '<div class="progress-labels">'
    for i, label in enumerate(PROGRESS_LABELS):
        cls = "done" if i + 1 < current_round else ("curr" if i + 1 == current_round else "")
        html += f'<span class="{cls}">{label}</span>'
    html += '</div>'

    html += '<div class="progress-steps">'
    for i in range(5):
        cls = ""
        if has_error and i + 1 == current_round:
            cls = "error"
        elif i + 1 < current_round:
            cls = "complete"
        elif i + 1 == current_round:
            cls = "active"
        html += f'<div class="progress-step {cls}"></div>'
    html += '</div>'

    # 状态 pill（四态：待命 / 推理中 / 方案已生成 / 航次不可行）
    if has_error:
        status_cls, status_text = "error", "航次不可行"
    elif current_round >= 5:
        status_cls, status_text = "success", "方案已生成"
    elif current_round == 0:
        status_cls, status_text = "idle", "待命"
    else:
        status_cls, status_text = "running", "推理中"
    html += f'<div class="decision-status {status_cls}" style="margin-top:8px;"><span></span>{status_text}</div>'
    return html


# ═══════════════════════════════════════════════════
# 2. 航线概览卡片（metro 地铁线）
# ═══════════════════════════════════════════════════

def build_route_overview(tool_results: dict = None) -> str:
    data = parse_route_overview(tool_results or {})
    seg_rows = parse_segment_rows(tool_results or {})

    origin = data.get("origin", "-")
    dest = data.get("destination", "-")
    lock_count = data.get("lock_count", "-")

    # 构建节点表：[origin] + [各航段终点]，去重
    nodes = [origin] if origin != "-" else []
    for r in seg_rows:
        d = str(r.get("destination", "")).strip()
        if d and (not nodes or d != nodes[-1]):
            nodes.append(d)
    if dest != "-" and (not nodes or nodes[-1] != dest):
        nodes.append(dest)

    html = '<div class="result-card">'
    html += '<div class="card-title">🗺️ 航线概览</div>'

    if not nodes:
        html += '<div class="summary-grid">'
        html += _summary_item(data.get("total_km", "-"), "总里程 (km)")
        html += _summary_item(data.get("segment_count", "-"), "航段数")
        html += _summary_item(data.get("lock_count", "-"), "途经船闸")
        html += _summary_item(data.get("lock_wait_h", "-"), "过闸等待 (h)")
        html += '</div></div>'
        return html

    # ship 起止百分比
    start_idx = nodes.index(origin) if origin in nodes else 0
    end_idx = nodes.index(dest) if dest in nodes else len(nodes) - 1
    max_idx = max(len(nodes) - 1, 1)

    stops_css = f"--stops:{max(len(nodes), 2)}; --ship-from:{start_idx / max_idx * 100:.2f}%; --ship-to:{end_idx / max_idx * 100:.2f}%"

    stops = ['<span class="metro-ship" aria-hidden="true"><span class="ship-emoji">🚢</span></span>']
    for i, node in enumerate(nodes):
        if i == start_idx:
            kind = "metro-origin"
        elif i == end_idx:
            kind = "metro-destination"
        else:
            kind = "metro-waypoint"
        distance = ""
        if i > 0 and i - 1 < len(seg_rows):
            dist = seg_rows[i - 1].get("distance_km", "-")
            if dist not in ("-", "", None):
                distance = f'<span class="metro-distance">{_escape(dist)} km</span>'
        stops.append(
            f'<div class="metro-stop {kind}">{distance}<span class="metro-dot"></span>'
            f'<span class="metro-port">{_escape(node)}</span></div>'
        )

    html += f'<div class="route-track"><div class="metro-route" style="{stops_css}">{"".join(stops)}</div>'
    html += f'<div class="segment-summary">'
    html += '<span class="route-key origin-key">起点</span>'
    html += '<span class="route-key waypoint-key">途经港口</span>'
    html += '<span class="route-key destination-key">终点</span>'
    if lock_count not in ("-", "", "0"):
        html += f'<span class="route-key">途经船闸 {_escape(lock_count)} 座</span>'
    html += f'<span class="route-key">{_escape(nodes[0])} → {_escape(nodes[-1])}</span>'
    html += '</div></div></div>'
    return html


# ═══════════════════════════════════════════════════
# 3. 航速方案表格 + 能耗柱状图
# ═══════════════════════════════════════════════════

def build_speed_table_html(tool_results: dict = None) -> str:
    rows = parse_speed_rows(tool_results or {})

    if not rows:
        return empty_card("📊 航速优化方案", "等待航速优化结果...")

    headers = ["航段", "起点→终点", "距离", "推荐航速", "行驶时间", "过闸等待", "预计电耗"]

    html = '<div class="result-card">'
    html += '<div class="card-title">📊 航速优化方案</div>'
    html += '<div class="speed-table-wrapper"><table class="speed-table"><thead><tr>'
    for h in headers:
        html += f'<th>{h}</th>'
    html += '</tr></thead><tbody>'

    for r in rows:
        seg_id = r.get("segment_id", "-")
        route = r.get("route", f"航段{seg_id}")
        dist = r.get("distance_km", "-")
        dist_str = f"{dist} km" if dist != "-" else "-"

        speed_kn = r.get("speed_kn", "-")
        speed_kmh = r.get("speed_kmh", "-")
        if speed_kmh != "-" and speed_kn != "-":
            speed_str = f'{speed_kmh} km/h（{speed_kn}节）'
        elif speed_kmh != "-":
            speed_str = f'{speed_kmh} km/h'
        elif speed_kn != "-":
            speed_str = f'{speed_kn}节'
        else:
            speed_str = "-"

        sail_h = r.get("sail_time_h", "-")
        sail_str = f"{sail_h} h" if sail_h != "-" else "-"

        lock_h = r.get("lock_wait_h", "0")
        lock_str = f"{lock_h} h" if lock_h not in ("0", "-", "0.0") else "-"

        energy = r.get("energy_kwh", "-")
        energy_str = f"{energy} kWh" if energy != "-" else "-"

        html += '<tr>'
        html += f'<td>航段{seg_id}</td>'
        html += f'<td>{_escape(route)}</td>'
        html += f'<td>{dist_str}</td>'
        html += f'<td>{speed_str}</td>'
        html += f'<td>{sail_str}</td>'
        html += f'<td>{lock_str}</td>'
        html += f'<td>{energy_str}</td>'
        html += '</tr>'

    html += '</tbody></table></div>'

    # ── 能耗柱状图（双指标：能耗 + 航速）──
    energies = []
    speeds = []
    for r in rows:
        e = r.get("energy_kwh", "-")
        s = r.get("speed_kmh", "-")
        try:
            energies.append(float(e))
        except (TypeError, ValueError):
            energies.append(0.0)
        try:
            speeds.append(float(s))
        except (TypeError, ValueError):
            speeds.append(0.0)
    max_energy = max(energies) if energies else 1

    html += f'<div class="energy-chart" style="margin-top:14px;">'
    html += '<div class="legend"><span class="legend-energy"></span> 推荐能耗 <span class="legend-speed"></span> 对地航速</div>'
    html += f'<div class="bar-chart" style="--count:{len(rows)}">'
    for i, r in enumerate(rows):
        height = max(8, (energies[i] / max(max_energy, 1)) * 100)
        html += (
            f'<div class="bar-group">'
            f'<span class="bar-metrics">'
            f'<span class="bar-speed-value">{_fmt(speeds[i])} km/h</span>'
            f'<span class="bar-value">{_fmt(energies[i])} kWh</span>'
            f'</span>'
            f'<div class="bar" style="height:{height:.1f}%"></div>'
            f'<span class="bar-label">{_escape(r.get("route", ""))}</span>'
            f'</div>'
        )
    html += '</div></div>'
    html += '</div>'
    return html


# ═══════════════════════════════════════════════════
# 4. 汇总信息卡片（KPI 四卡）
# ═══════════════════════════════════════════════════

def build_summary_card(tool_results: dict = None) -> str:
    data = parse_summary(tool_results or {})

    total_time = data.get("total_time_h", "-")
    total_energy = data.get("total_energy_kwh", "-")
    arrival_soc = data.get("arrival_soc_pct", "-")
    saving_pct = data.get("saving_pct", "-")
    risk = data.get("risk_level", "-")

    time_str = f"{total_time} h" if total_time != "-" else "--"
    energy_str = f"{total_energy} kWh" if total_energy != "-" else "--"
    soc_str = f"{arrival_soc}%" if arrival_soc != "-" else "--"
    save_str = f"{saving_pct}%" if saving_pct != "-" else "--"

    html = '<div class="result-card">'
    html += '<div class="card-title">⚡ 汇总信息</div>'
    html += '<div class="kpi-grid" style="padding:10px 0 0;">'
    html += _kpi_item("计划耗时", time_str, "时间约束校验")
    html += _kpi_item("预计总能耗", energy_str, "优化后总电耗")
    html += _kpi_item("到港 SOC", soc_str, "双电池平均值")
    html += _kpi_item("节能效果", save_str, "相对历史航速")
    html += '</div>'

    if risk != "-":
        risk_cls = "error" if risk == "不可行" else ("warn" if risk == "边界" else "ok")
        html += f'<div style="margin-top:8px;"><span class="badge badge-{risk_cls}">风险：{_escape(risk)}</span></div>'

    html += '</div>'
    return html


# ═══════════════════════════════════════════════════
# 5. 安全校验面板（safety-list 两列）
# ═══════════════════════════════════════════════════

def build_safety_panel(tool_results: dict = None) -> str:
    checks = parse_safety(tool_results or {})

    html = '<div class="result-card">'
    html += '<div class="card-title">🛡️ 安全校验</div>'
    if not checks:
        html += '<div class="empty-inline"><span class="empty-icon">🛡️</span><span>暂无校验结果</span></div>'
        html += '</div>'
        return html

    html += '<div class="safety-list">'
    for c in checks:
        status = c.get("status", "unknown")
        name = str(c.get("name", ""))
        text = str(c.get("text", ""))
        category = "benefit" if ("节能" in name or "续航" in name) else ("soc" if "SOC" in name else "state")
        html += (
            f'<div class="safety-item {_escape(status)} {category}">'
            f'<span class="safety-label">{_escape(name)}</span>'
            f'<span class="safety-content">{_escape(text)}</span>'
            f'</div>'
        )
    html += '</div></div>'
    return html


# ═══════════════════════════════════════════════════
# 6. 能效管理建议（recommendation-list）
# ═══════════════════════════════════════════════════

def build_recommendations(tool_results: dict = None) -> str:
    items = parse_recommendations(tool_results or {})

    html = '<div class="result-card">'
    html += '<div class="card-title">💡 能效管理建议</div>'
    if not items:
        html += '<div class="empty-inline"><span class="empty-icon">💡</span><span>等待 EMS 仿真结果</span></div>'
        html += '</div>'
        return html

    html += '<div class="recommendation-list">'
    for item in items:
        cls = item.get("type", "info")
        text = item.get("text", "")[:250]
        html += f'<div class="recommendation {_escape(cls)}">{_escape(text)}</div>'
    html += '</div></div>'
    return html


# ═══════════════════════════════════════════════════
# 7. 不可行原因提示横幅（Figma 三段式）
# ═══════════════════════════════════════════════════

def build_infeasible_alert(tool_results: dict = None, raw_text: str = "") -> str:
    """当航次不可行时，独立展示原因和补救建议。支持 raw_text 用于前置检验场景"""
    detail = parse_infeasible_detail(tool_results or {}, raw_text=raw_text)

    if not detail.get("is_infeasible"):
        return ""

    title = detail.get("title", "航次不可行")

    html = '<div class="infeasible-alert">'
    html += (
        '<header><span class="infeasible-icon">⛔</span>'
        '<div><span class="surface-kicker">VOYAGE ALERT</span>'
        f'<h3>{_escape(title)}</h3></div></header>'
    )
    html += '<div class="infeasible-content">'

    # 原因列表
    causes = detail.get("causes", [])
    if causes:
        html += '<div class="infeasible-block"><strong>原因分析</strong>'
        for c in causes:
            html += f'<p>{_escape(c[:200])}</p>'
        html += '</div>'

    # 缺口
    gap = detail.get("gap", "")
    if gap:
        html += f'<div class="infeasible-block"><strong>缺口</strong><p>{_escape(gap)}</p></div>'

    # 建议
    suggestions = detail.get("suggestions", [])
    if suggestions:
        html += '<div class="infeasible-block suggestion"><strong>补救建议</strong>'
        for s in suggestions:
            html += f'<p>{_escape(s[:200])}</p>'
        html += '</div>'

    html += '</div></div>'
    return html


# ═══════════════════════════════════════════════════
# 8. 历史对话面板
# ═══════════════════════════════════════════════════

def build_history_html(history_items: list) -> str:
    """渲染历史对话面板"""
    if not history_items:
        return '<div class="empty-inline" style="min-height:80px;"><span class="empty-icon">📜</span><span>暂无历史对话</span></div>'

    html = '<div class="history-list">'

    for idx, item in enumerate(history_items, start=1):
        user = _escape(str(item.get("user", ""))[:500])
        enhanced = _escape(str(item.get("enhanced", ""))[:500])
        status = item.get("status", "-")
        final_answer = _escape(str(item.get("final_answer", "本轮未返回最终摘要"))[:1200])

        if status == "infeasible":
            status_cls, status_text = "error", "不可行"
        elif status == "feasible":
            status_cls, status_text = "ok", "可行"
        else:
            status_cls, status_text = "unknown", str(status)

        html += '<div class="history-item">'
        html += '<div class="history-header">'
        html += f'<span class="history-title">第 {idx} 次对话</span>'
        html += f'<span class="badge badge-{status_cls}">{_escape(status_text)}</span>'
        html += '</div>'

        html += '<div class="history-block">'
        html += '<div class="history-label">用户输入</div>'
        html += f'<div class="history-text">{user}</div>'
        html += '</div>'

        if enhanced and enhanced != user:
            html += '<div class="history-block">'
            html += '<div class="history-label">增强输入</div>'
            html += f'<div class="history-text">{enhanced}</div>'
            html += '</div>'

        html += '<div class="history-block">'
        html += '<div class="history-label">最终结果</div>'
        html += f'<div class="history-answer">{final_answer}</div>'
        html += '</div>'

        html += '</div>'

    html += '</div>'
    return html


# ═══════════════════════════════════════════════════
# 9. 推理轮次卡片
# ═══════════════════════════════════════════════════

def build_round_card(round_num: int, tool_name: str, thought: str,
                     action_params: dict, observation: str,
                     is_final: bool = False, is_infeasible: bool = False) -> str:
    icon = TOOL_ICONS.get(tool_name, "🔧")
    name_zh = TOOL_NAMES_ZH.get(tool_name, tool_name)
    tool_badge = f"{icon} {name_zh}" if tool_name else ""

    if is_infeasible:
        html = '<div class="react-round" style="border-left-color:var(--red);background:var(--red-soft);">'
    else:
        html = '<div class="react-round">'
    html += f'<div class="round-header">'
    html += f'<span class="round-label">第{round_num}轮</span>'
    if tool_badge:
        html += f'<span class="tool-badge">{tool_badge}</span>'
    html += f'</div>'

    if thought:
        html += f'<div class="thought-text">💭 {_escape(thought[:300])}</div>'

    if action_params:
        params_str = ", ".join(f"{k}={v}" for k, v in list(action_params.items())[:6])
        html += f'<div class="action-text">▶ {_escape(tool_name)}({_escape(params_str)})</div>'

    if observation:
        obs = observation[:2000]
        if len(observation) > 2000:
            obs += f"\n... （共{len(observation)}字符，尾部已截断）"
        html += f'<div class="obs-block">{_escape(obs)}</div>'

    if is_final and not is_infeasible:
        html += f'<div style="padding:6px 12px;color:var(--teal);font-weight:600;font-size:0.82rem;">✅ 推理完成</div>'
    elif is_infeasible:
        html += f'<div style="padding:6px 12px;color:var(--red);font-weight:600;font-size:0.82rem;">⛔ 不可行</div>'

    html += '</div>'
    return html


def build_final_card(final_answer: str, status: str = "feasible") -> str:
    """用于推理轨迹区的最终结果卡片"""
    if not final_answer:
        return ""

    border = "var(--red)" if status == "infeasible" else "var(--teal)"
    bg = "var(--red-soft)" if status == "infeasible" else "#f0fdf4"
    icon = "⛔" if status == "infeasible" else "✅"
    title = "不可行" if status == "infeasible" else "最终方案"

    html = f'<div class="react-round" style="border-left-color:{border};background:{bg};">'
    html += f'<div class="round-header">'
    html += f'<span class="round-label" style="color:{border};">{icon} {title}</span>'
    html += f'</div>'
    html += f'<div class="obs-block">{_escape(final_answer[:1500])}</div>'
    html += f'</div>'
    return html


# ═══════════════════════════════════════════════════
# 8. 推理轨迹容器
# ═══════════════════════════════════════════════════

def build_trace_html(cards_by_round: dict) -> str:
    """将覆盖式卡片 dict 拼接为完整推理轨迹 HTML"""
    if not cards_by_round:
        return '<div class="empty-inline" style="min-height:80px;"><span class="empty-icon">🧠</span><span>推理开始后将在这里显示 Thought / Action / Observation</span></div>'
    return "".join(cards_by_round[k] for k in sorted(cards_by_round))


# ═══════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════

def _kpi_item(label: str, value: str, hint: str) -> str:
    return (
        f'<div class="kpi-card">'
        f'<span class="kpi-label">{_escape(label)}</span>'
        f'<strong>{_escape(value)}</strong>'
        f'<small>{_escape(hint)}</small>'
        f'</div>'
    )


def _summary_item(value: str, label: str, color_class: str = "") -> str:
    return (
        f'<div class="summary-item">'
        f'<div class="value {color_class}">{value}</div>'
        f'<div class="label">{label}</div>'
        f'</div>'
    )


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))
