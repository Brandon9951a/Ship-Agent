"""
ui/theme.py — 智行合一 自定义主题
白色基地 · 蓝色配色 · 桌面端控制台 Dashboard
2026-08-02 整改：引入设计令牌（CSS 变量）+ Figma 参考设计系统
"""
import gradio as gr

# ═══════════════════════════════════════════════════
# 设计令牌（单一事实源，CSS 中的 var() 与之对应）
# ═══════════════════════════════════════════════════

TOKENS = {
    "ink": "#17212b",          # 正文
    "ink-soft": "#334155",     # 次级文字
    "muted": "#697786",        # 弱化文字
    "muted-weak": "#94a3b8",   # 占位/禁用
    "line": "#dce3e9",         # 分割线/卡片边框
    "line-strong": "#cbd5e1",  # 输入框边框
    "line-soft": "#f1f5f9",    # 表格行分隔
    "canvas": "#f4f7f8",       # 页面背景
    "surface": "#ffffff",      # 卡片背景
    "blue": "#1769d4",
    "blue-deep": "#1158b5",    # hover
    "blue-soft": "#e8f1ff",
    "teal": "#0f9d84",
    "teal-deep": "#087c68",
    "teal-soft": "#e6f7f2",
    "amber": "#c77818",
    "amber-soft": "#fff4dc",
    "red": "#c53d43",
    "red-soft": "#fff0f1",
    "radius": "8px",
}


def get_theme() -> gr.Theme:
    return gr.themes.Soft(
        primary_hue="blue",
        secondary_hue="slate",
        neutral_hue="slate",
        font=gr.themes.GoogleFont("Inter"),
    ).set(
        background_fill_primary="*neutral_50",
        background_fill_secondary="white",
        block_background_fill="white",
        block_border_width="1px",
        block_border_color="*neutral_200",
        block_radius="12px",
        block_title_text_color="*neutral_800",
        input_background_fill="white",
        input_border_color="*neutral_300",
        input_border_color_focus="*primary_500",
        input_radius="10px",
        input_shadow_focus="*primary_50",
        button_primary_background_fill="*primary_600",
        button_primary_background_fill_hover="*primary_700",
        button_primary_text_color="white",
        button_secondary_background_fill="white",
        button_secondary_background_fill_hover="*neutral_100",
        button_secondary_text_color="*neutral_700",
        button_secondary_border_color="*neutral_300",
        layout_gap="12px",
    )


CSS = """
/* ══════════════════════════════════════════════════════
   智行合一 UI — 白色基地 · 蓝色配色 · 桌面端 Dashboard
   设计令牌（CSS 变量）注入 Gradio 容器根
   ══════════════════════════════════════════════════════ */
.gradio-container {
    /* 设计令牌 */
    --ink: #17212b;
    --ink-soft: #334155;
    --muted: #697786;
    --muted-weak: #94a3b8;
    --line: #dce3e9;
    --line-strong: #cbd5e1;
    --line-soft: #f1f5f9;
    --canvas: #f4f7f8;
    --surface: #ffffff;
    --blue: #1769d4;
    --blue-deep: #1158b5;
    --blue-soft: #e8f1ff;
    --teal: #0f9d84;
    --teal-deep: #087c68;
    --teal-soft: #e6f7f2;
    --amber: #c77818;
    --amber-soft: #fff4dc;
    --red: #c53d43;
    --red-soft: #fff0f1;
    --radius: 8px;

    max-width: 1680px !important;
    width: min(96vw, 1680px) !important;
    margin: 0 auto !important;
    padding: 12px 16px 20px !important;
    background: var(--canvas) !important;
    font-family: Inter, "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif;
}
.gradio-container .gap { gap: 12px !important; }

/* ── 顶栏（Figma 风格）── */
.topbar {
    min-height: 82px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    padding: 0 4px;
    border-bottom: 1px solid var(--line);
}
.brand-lockup { display: flex; align-items: center; gap: 10px; }
.brand-mark {
    position: relative; width: 42px; height: 40px;
    display: grid; place-items: center; color: var(--blue);
}
.brand-mark .brand-emoji { position: relative; z-index: 1; font-size: 24px; line-height: 1; }
.brand-mark span {
    position: absolute; bottom: 4px; width: 34px; height: 5px;
    border-top: 2px solid var(--teal); border-bottom: 2px solid #8fc7ef;
    border-radius: 50%; opacity: .9;
}
.brand-title h1 { margin: 0; font-size: 18px; font-weight: 700; color: var(--ink); }
.eyebrow, .surface-kicker {
    margin: 0; color: #778692;
    font-size: 10px; font-weight: 700; letter-spacing: .11em;
}
.brand-title .eyebrow { margin-bottom: 2px; }
.topbar-meta { display: flex; align-items: center; gap: 18px; color: var(--muted); font-size: 12px; }
.connection { display: flex; align-items: center; gap: 7px; }
.status-dot {
    width: 7px; height: 7px; background: var(--teal);
    border-radius: 50%; box-shadow: 0 0 0 4px var(--teal-soft);
}
.status-dot.offline { background: #aebbc5; box-shadow: 0 0 0 4px #eef2f4; }
.clock { font-variant-numeric: tabular-nums; }

/* ── 顶部电脑端两栏 ── */
.desktop-top-grid { align-items: stretch !important; gap: 12px !important; }

/* ── 左侧输入面板（Figma 风格）── */
.mission-panel {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 18px;
    box-shadow: 0 1px 4px rgba(23, 33, 43, 0.05);
}
.input-panel { background: white; border: 1px solid var(--line); border-radius: 16px; padding: 14px 16px; box-shadow: 0 1px 4px rgba(23, 33, 43, 0.05); }
.panel-kicker { display: flex; align-items: center; gap: 7px; color: var(--blue); font-size: 12px; font-weight: 700; }
.panel-heading { font-size: 0.95rem; font-weight: 800; color: var(--ink-soft); margin-bottom: 10px; }
.mission-panel h2 { margin: 11px 0 6px; font-size: 19px; color: var(--ink); }
.panel-copy { margin-bottom: 18px; color: var(--muted); font-size: 12px; line-height: 1.65; }
.field-label { display: block; margin-bottom: 7px; color: #425362; font-size: 12px; font-weight: 600; }

/* ── 表单控件 ── */
.input-box textarea {
    min-height: 120px !important; max-height: 160px !important;
    border: 2px solid var(--line-strong) !important; border-radius: 12px !important;
    padding: 12px 14px !important; font-size: 0.9rem !important;
    resize: vertical !important;
    transition: border-color 0.2s ease !important;
    color: var(--ink) !important; background: var(--surface) !important;
}
.input-box textarea:focus {
    border-color: var(--blue) !important;
    box-shadow: 0 0 0 3px rgba(23, 105, 212, 0.10) !important;
    outline: none !important;
}
.action-row { margin-top: 6px; gap: 8px !important; }

/* ── 按钮 ── */
.btn-submit {
    background: linear-gradient(135deg, var(--blue), var(--blue-deep)) !important;
    border: none !important; color: white !important;
    font-weight: 700 !important; padding: 10px 24px !important;
    border-radius: 10px !important; transition: all 0.2s !important;
}
.btn-submit:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(23, 105, 212, 0.35) !important; }
.icon-button {
    width: 32px; height: 32px; display: inline-grid; place-items: center;
    color: var(--blue); background: var(--blue-soft);
    border: 1px solid #cddfff; border-radius: 6px; cursor: pointer; font-size: 15px;
}
.icon-button:hover { background: #dbeaff; }
.icon-button.recording {
    color: white; background: var(--red); border-color: var(--red);
    box-shadow: 0 0 0 4px rgba(197, 61, 67, .13);
}
.text-button {
    display: inline-flex; align-items: center; gap: 5px; padding: 5px 0;
    color: var(--muted); background: transparent; border: 0;
    font-size: 11px; cursor: pointer;
}
.voice-copy { flex: 1; color: var(--muted); font-size: 11px; }
.input-actions { display: flex; align-items: center; min-height: 38px; gap: 8px; margin: 8px 0 16px; }
.run-button {
    width: 100%; display: inline-flex; align-items: center; justify-content: center; gap: 7px;
    color: white; background: var(--blue); border: 1px solid var(--blue);
    border-radius: 6px; font-weight: 700; height: 42px; margin-top: 18px; font-size: 13px; cursor: pointer;
}
.run-button:hover { background: var(--blue-deep); }
.run-button:disabled { opacity: .56; cursor: not-allowed; }

/* ── 快捷航线表单 ── */
.route-form { padding-top: 15px; border-top: 1px solid var(--line); }
.section-label { margin-bottom: 10px; color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .08em; }
.route-form label { display: block; margin-top: 11px; }
.route-form label > span:first-child { display: block; margin-bottom: 7px; color: #425362; font-size: 12px; font-weight: 600; }
.route-form select, .route-form input { height: 35px; padding: 0 9px; font-size: 12px; }
.route-pair { display: flex; align-items: center; gap: 8px; }
.route-pair label { flex: 1; min-width: 0; }
.route-arrow {
    display: flex; align-items: center; justify-content: center;
    width: 32px; height: 35px;
    color: var(--muted); font-size: 18px; font-weight: 600;
}
.time-field { display: flex !important; justify-content: space-between; align-items: center; }
.time-field > span:first-child { margin-bottom: 0 !important; }
.stepper { display: inline-flex; align-items: center; gap: 5px; }
.stepper button {
    width: 27px; height: 27px; padding: 0; color: var(--blue);
    background: var(--blue-soft); border: 1px solid #cddfff;
    border-radius: 5px; font-size: 17px; line-height: 1; cursor: pointer;
}
.stepper input { width: 46px; height: 27px; padding: 0; text-align: center; font-weight: 700; }
.stepper em { color: var(--muted); font-size: 12px; font-style: normal; }
.primary-button {
    width: 100%; display: inline-flex; align-items: center; justify-content: center; gap: 7px;
    color: white; background: var(--blue); border: 1px solid var(--blue);
    border-radius: 6px; font-weight: 700; height: 35px; margin-top: 14px; font-size: 12px; cursor: pointer;
}
.primary-button:hover { background: var(--blue-deep); }

/* ── 快捷示例 ── */
.example-title { margin: 14px 0 6px; font-size: 0.82rem; font-weight: 700; color: #475569; }
.example-stack { gap: 6px !important; }
.sample-block { margin-top: 15px; }
.sample-list { display: grid; gap: 5px; }
.sample-list button {
    display: flex; justify-content: space-between; padding: 8px 9px;
    color: #465766; text-align: left; background: #f8fafb;
    border: 1px solid #e5eaee; border-radius: 6px; font-size: 11px; cursor: pointer;
}
.sample-list button:hover { color: var(--blue); border-color: #bbd4f7; background: var(--blue-soft); }
.sample-list span { color: #84929e; }

/* ── 高级参数 / Accordion ── */
.advanced-panel { margin-top: 14px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.gr-accordion { border: 1px solid var(--line) !important; border-radius: 10px !important; background: white !important; }

/* ── 右侧状态面板 ── */
.status-panel { display: flex; flex-direction: column; gap: 10px; }

/* ── 决策头部 ── */
.decision-heading {
    display: flex; align-items: center; justify-content: space-between; gap: 20px;
    min-height: 72px; padding: 3px 0 17px; border-bottom: 1px solid var(--line);
}
.decision-heading h2 { margin: 5px 0 3px; font-size: 22px; color: var(--ink); }
.decision-heading p:last-child { margin: 0; color: var(--muted); font-size: 12px; }
.decision-status {
    display: inline-flex; align-items: center; gap: 7px; padding: 7px 10px;
    color: var(--muted); background: #edf1f4; border-radius: 999px;
    font-size: 12px; font-weight: 600; white-space: nowrap;
}
.decision-status span { width: 7px; height: 7px; background: currentColor; border-radius: 50%; }
.decision-status.running { color: var(--blue); background: var(--blue-soft); }
.decision-status.success { color: var(--teal-deep); background: var(--teal-soft); }
.decision-status.error { color: var(--red); background: var(--red-soft); }

/* ── 结果卡片 ── */
.result-card {
    background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
    padding: 14px 16px; margin: 0; box-shadow: 0 1px 4px rgba(23, 33, 43, 0.055);
}
.result-card .card-title {
    font-size: 0.92rem; font-weight: 800; color: var(--ink);
    margin: 0 0 10px 0; padding-bottom: 8px; border-bottom: 1px solid var(--line);
}
.compact-card { padding: 12px 14px; }
.summary-card { min-height: 126px; }
.equal-card { min-height: 146px; }
.mini-card-row { align-items: stretch !important; gap: 12px !important; }

/* ── KPI 卡片（Figma 风格，左对齐）── */
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; padding: 16px 0; }
.kpi-card {
    min-height: 103px; padding: 14px;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius);
}
.kpi-label { display: block; color: var(--muted); font-size: 11px; }
.kpi-card strong {
    display: block; margin: 11px 0 4px; color: var(--ink);
    font-size: 22px; line-height: 1; font-variant-numeric: tabular-nums;
}
.kpi-card:nth-child(2) strong { color: var(--teal); }
.kpi-card small { color: #8b99a4; font-size: 10px; }

/* ── 汇总网格（兼容旧组件）── */
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.summary-item {
    background: var(--canvas); border: 1px solid var(--line-soft);
    border-radius: 12px; padding: 10px 8px; text-align: center; min-width: 0;
}
.summary-item .value {
    font-size: clamp(1.05rem, 1.35vw, 1.45rem); font-weight: 850;
    color: var(--ink); line-height: 1.15;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.summary-item .value.blue { color: var(--blue); }
.summary-item .value.green { color: #16a34a; }
.summary-item .value.orange { color: #ea580c; }
.summary-item .label { font-size: 0.68rem; color: var(--muted); margin-top: 4px; font-weight: 600; }

/* ── 空状态 ── */
.empty-state {
    color: var(--muted-weak); padding: 14px; background: var(--canvas);
    border: 1px dashed var(--line-strong); border-radius: 12px;
    text-align: center; font-size: 0.84rem;
}
.compact-empty { padding: 10px; }
.empty-inline {
    min-height: 110px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 8px;
    color: var(--muted-weak); font-size: 11px; text-align: center;
}
.empty-inline .empty-icon { font-size: 20px; line-height: 1; }

/* ── Surface 卡片（Figma 风格）── */
.analysis-grid, .bottom-grid { display: grid; grid-template-columns: 1.1fr .9fr; gap: 12px; }
.surface {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); min-width: 0;
}
.surface-header {
    display: flex; align-items: center; justify-content: space-between;
    min-height: 59px; padding: 13px 15px; border-bottom: 1px solid var(--line);
}
.surface-header h3 { margin: 3px 0 0; font-size: 14px; color: var(--ink); }
.surface-kicker { font-size: 9px; }
.route-chip, .result-count {
    padding: 4px 7px; color: #597084; background: #eef3f6;
    border-radius: 4px; font-size: 10px; font-weight: 600;
}

/* ── 航线 Metro 图（Figma 风格）── */
.route-track { min-height: 132px; padding: 18px 15px 8px; }
.metro-route {
    position: relative; display: grid;
    grid-template-columns: repeat(var(--stops), minmax(50px, 1fr));
    align-items: start; min-width: 100%; padding-top: 18px;
}
.metro-stop { position: relative; z-index: 1; min-height: 84px; text-align: center; }
.metro-stop::before {
    content: ""; position: absolute; top: 9px; right: 50%;
    width: 100%; height: 3px; background: #d5dee5; z-index: -1;
}
.metro-stop:first-of-type::before { display: none; }
.metro-stop.metro-waypoint::before, .metro-stop.metro-destination::before { background: #86b7f5; }
.metro-dot {
    width: 19px; height: 19px; display: inline-block;
    border: 4px solid #fff; background: #aebbc5;
    border-radius: 50%; box-shadow: 0 0 0 2px #aebbc5;
}
.metro-origin .metro-dot { background: var(--teal); box-shadow: 0 0 0 2px var(--teal); }
.metro-waypoint .metro-dot { background: var(--blue); box-shadow: 0 0 0 2px var(--blue); }
.metro-destination .metro-dot { background: #e0784c; box-shadow: 0 0 0 2px #e0784c; }
.metro-distance {
    position: absolute; top: -18px; right: 50%; width: 100%;
    color: #7e8e9a; font-size: 9px; font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.metro-port {
    display: block; margin: 9px auto 0; max-width: 66px;
    color: #4d6171; font-size: 10px; line-height: 1.35; word-break: break-all;
}
.metro-origin .metro-port { color: var(--teal-deep); font-weight: 700; }
.metro-destination .metro-port { color: #c15a33; font-weight: 700; }
.metro-off-route .metro-port { color: #9aa7b2; }
.metro-ship {
    position: absolute; z-index: 3; top: -4px; left: var(--ship-from);
    width: 22px; height: 22px; display: grid; place-items: center;
    color: var(--blue); background: #fff; border: 1px solid #bad5fa;
    border-radius: 50%; box-shadow: 0 2px 7px rgba(30, 91, 153, .18);
    transform: translateX(-50%); animation: metro-sail 5s ease-in-out infinite;
}
.metro-ship .ship-emoji { font-size: 12px; line-height: 1; }
@keyframes metro-sail {
    0%, 14% { left: var(--ship-from); opacity: 0; }
    22% { opacity: 1; }
    68% { left: var(--ship-to); opacity: 1; }
    80%, 100% { left: var(--ship-to); opacity: 0; }
}
.segment-summary { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 15px 16px; }
.route-key {
    padding: 5px 7px; color: #5f7080; background: var(--canvas);
    border-radius: 4px; font-size: 10px;
}
.origin-key { color: var(--teal-deep); background: var(--teal-soft); }
.waypoint-key { color: var(--blue); background: var(--blue-soft); }
.destination-key { color: #bd552e; background: #fff2ec; }

/* ── 能耗柱状图（Figma 风格）── */
.energy-chart { min-height: 183px; padding: 13px 15px; }
.legend { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 11px; white-space: nowrap; }
.legend-energy, .legend-speed { width: 9px; height: 9px; display: inline-block; }
.legend-energy { background: var(--blue); border-radius: 2px; }
.legend-speed { margin-left: 5px; background: #e0784c; border-radius: 50%; }
.bar-chart {
    display: grid; grid-template-columns: repeat(var(--count), minmax(40px, 1fr));
    align-items: end; gap: 14px; height: 154px;
    padding-top: 12px; border-bottom: 1px solid #dfe6eb;
}
.bar-group {
    height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: flex-end; gap: 6px;
}
.bar-metrics { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.bar-speed-value { color: #c65b32; font-size: 10px; font-weight: 700; white-space: nowrap; }
.bar-value { color: #40586c; font-size: 10px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.bar {
    width: min(40px, 64%); min-height: 7px;
    background: linear-gradient(180deg, #4e91f1, var(--blue));
    border-radius: 5px 5px 0 0;
}
.bar-label {
    max-width: 100%; overflow: hidden; color: #687887;
    font-size: 9px; text-overflow: ellipsis; white-space: nowrap;
}

/* ── 方案表格 ── */
.table-surface { margin-top: 12px; }
.table-scroll { width: 100%; overflow-x: auto; }
.speed-table-wrapper { width: 100%; overflow-x: auto; margin-top: 10px; }
.speed-table { width: 100%; min-width: 780px; border-collapse: collapse; }
.speed-table th, .speed-table td { padding: 14px 16px; border-bottom: 1px solid var(--line-soft); text-align: left; white-space: nowrap; font-size: 13px; }
.speed-table th { color: #71808c; background: #fafbfc; font-size: 11px; font-weight: 700; letter-spacing: .03em; }
.speed-table td { color: var(--ink-soft); height: 48px; }
.speed-table td:first-child { color: var(--ink); font-weight: 700; }
.speed-table td:nth-child(4) { color: var(--blue); font-weight: 700; }
.speed-table td:last-child { color: var(--teal-deep); font-weight: 700; }
.speed-table .placeholder-row td { padding: 25px; color: var(--muted-weak); text-align: center; font-weight: 400; }
.speed-table tr:hover td { background: #fafbfc; }
/* Figma 原版 .table-surface（保留给未来迁移用） */
.table-surface table { width: 100%; min-width: 720px; border-collapse: collapse; }
.table-surface th, .table-surface td { padding: 16px 18px; border-bottom: 1px solid var(--line-soft); text-align: left; white-space: nowrap; font-size: 14px; }
.table-surface th { color: #71808c; background: #fafbfc; font-size: 12px; font-weight: 700; letter-spacing: .03em; }
.table-surface td { color: var(--ink-soft); height: 54px; }
.table-surface td:first-child { font-weight: 700; }
.table-surface td:nth-child(3) { color: var(--blue); font-weight: 700; }
.table-surface td:last-child { color: var(--teal-deep); font-weight: 700; }
.table-surface .placeholder-row td { padding: 25px; color: var(--muted-weak); text-align: center; }
.table-surface tr:hover td { background: #fafbfc; }

/* ── 进度指示器 ── */
.progress-labels { display: flex; gap: 4px; margin-bottom: 4px; }
.progress-labels span {
    flex: 1; font-size: 0.64rem; color: var(--muted-weak); text-align: center;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.progress-labels span.done { color: var(--teal); font-weight: 700; }
.progress-labels span.curr { color: var(--blue); font-weight: 800; }
.progress-steps { display: flex; gap: 5px; margin-bottom: 6px; }
.progress-step { flex: 1; height: 5px; border-radius: 3px; background: var(--line-strong); transition: all 0.3s ease; }
.progress-step.active { background: var(--blue); box-shadow: 0 0 3px rgba(23, 105, 212, 0.35); }
.progress-step.complete { background: var(--teal); }
.progress-step.error { background: var(--red); }

/* ── 状态徽章 ── */
.badge-row { display: flex; flex-wrap: wrap; gap: 6px; }
.badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 4px 12px; border-radius: 14px;
    font-size: 0.74rem; font-weight: 700; white-space: nowrap;
}
.badge-ok { background: #dcfce7; color: #16a34a; }
.badge-warn { background: #fff7ed; color: #ea580c; }
.badge-error { background: #fef2f2; color: #dc2626; }
.badge-unknown { background: var(--line-soft); color: var(--muted); }

/* ── 建议列表 ── */
.advice-list { list-style: none; padding: 0; margin: 0; }
.advice-list li {
    padding: 8px 12px; margin: 3px 0; background: var(--canvas);
    border-radius: 8px; font-size: 0.82rem; color: var(--ink-soft);
    line-height: 1.5; border-left: 3px solid var(--line);
}
.advice-list li.warn { border-left-color: var(--amber); background: var(--amber-soft); }
.advice-list li.ok { border-left-color: var(--teal); background: #f0fdf4; }
.advice-list li.info { border-left-color: var(--blue); background: var(--blue-soft); }
.advice-list li.error { border-left-color: var(--red); background: var(--red-soft); }

/* ── 安全校验（Figma 风格）── */
.bottom-grid { margin-top: 12px; grid-template-columns: .86fr 1.14fr; }
.safety-surface, .recommendation-surface { min-height: 288px; }
.safety-list {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px; min-height: 210px; padding: 16px 20px;
}
.safety-item {
    min-height: 82px; display: flex; align-items: center; justify-content: space-between;
    gap: 14px; padding: 14px 16px;
    border-left: 4px solid #91a1ad; background: #f7f9fa; border-radius: 4px;
}
.safety-item.ok { border-left-color: var(--teal); }
.safety-item.warn { border-left-color: var(--amber); }
.safety-item.error { border-left-color: var(--red); }
.safety-item .safety-label {
    display: block; flex: 0 0 auto; color: var(--ink);
    font-size: 15px; font-weight: 700; white-space: nowrap;
}
.safety-item .safety-content {
    display: block; min-width: 0; margin: 0; color: var(--blue);
    font-size: 18px; font-weight: 750; line-height: 1.25;
    text-align: right; white-space: nowrap;
}
.safety-item.benefit .safety-content, .safety-item.soc .safety-content { font-size: 20px; }
.safety-item.benefit .safety-content { color: #07845f; }
.safety-item.error .safety-content, .safety-item.error .safety-content * { color: var(--red); }
.safety-item .safety-content span { display: inline; font-size: inherit; font-weight: inherit; line-height: inherit; white-space: nowrap; }

/* ── 能效管理建议（Figma 风格）── */
.recommendation-list { display: grid; gap: 9px; min-height: 210px; padding: 16px 20px; }
.recommendation {
    padding: 12px 14px; color: #4a5d6b; background: #f7f9fa;
    border-left: 4px solid #9eabb5; border-radius: 4px;
    font-size: 13px; line-height: 1.65;
}
.recommendation.ok { border-left-color: var(--teal); }
.recommendation.warn { border-left-color: var(--amber); background: #fffaf0; }
.recommendation.error { border-left-color: var(--red); background: var(--red-soft); }
.recommendation.info { border-left-color: var(--blue); background: var(--blue-soft); }

/* ── 不可行提示（Figma 三段式）── */
.infeasible-alert {
    margin-top: 18px; overflow: hidden;
    background: #fff7f7; border: 1px solid #f2a3a7; border-radius: var(--radius);
}
.infeasible-alert header {
    display: flex; align-items: center; gap: 10px;
    min-height: 64px; padding: 12px 18px;
    background: var(--red-soft); border-bottom: 1px solid #f4c3c5;
}
.infeasible-icon {
    width: 28px; height: 28px; display: grid; place-items: center;
    color: var(--red); background: #fff; border-radius: 50%; font-size: 15px;
}
.infeasible-alert h3 { margin: 3px 0 0; color: #b5262e; font-size: 19px; }
.infeasible-alert .surface-kicker { color: var(--red); }
.infeasible-content { display: grid; gap: 12px; padding: 15px 18px 18px; }
.infeasible-block {
    padding: 11px 13px; color: #a72b31; background: #fff;
    border-left: 4px solid #d94c54; border-radius: 4px;
}
.infeasible-block strong { display: block; margin-bottom: 5px; color: #b5262e; font-size: 13px; }
.infeasible-block p { margin: 0; color: #a72b31; font-size: 14px; font-weight: 600; line-height: 1.65; }
.infeasible-block.suggestion { border-left-color: #3e83dd; background: #f1f7ff; }
.infeasible-block.suggestion strong, .infeasible-block.suggestion p { color: #2168c8; }
/* 兼容旧结构（callbacks 内部 fallback） */
.infeasible-header { font-size: 1rem; font-weight: 800; color: var(--red); margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
.infeasible-section { margin: 8px 0; }
.infeasible-label { font-size: 0.8rem; font-weight: 700; color: #991b1b; margin-bottom: 4px; }
.infeasible-list { list-style: none; padding: 0; margin: 0; }
.infeasible-list li { padding: 5px 10px; margin: 2px 0; background: white; border-radius: 6px; font-size: 0.8rem; color: #7f1d1d; border-left: 3px solid #fca5a5; }
.infeasible-list li::before { content: "• "; color: var(--red); font-weight: bold; }
.infeasible-list.suggestions li { border-left-color: #93c5fd; color: #1e40af; background: var(--blue-soft); }
.infeasible-list.suggestions li::before { content: "→ "; color: var(--blue); }
.infeasible-gap { margin: 8px 0; padding: 8px 12px; background: #fee2e2; border-radius: 8px; font-size: 0.85rem; font-weight: 700; color: var(--red); text-align: center; }

/* ── 最终方案卡片 + 复制按钮 ── */
.final-answer-card { position: relative; }
.final-answer-card .card-title { display: flex; align-items: center; justify-content: space-between; }
.copy-button {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 4px 10px; color: var(--muted); background: transparent;
    border: 1px solid var(--line); border-radius: 6px;
    font-size: 11px; cursor: pointer; font-weight: 600;
}
.copy-button:hover { color: var(--blue); border-color: #cddfff; background: var(--blue-soft); }
.final-answer-body { font-size: 0.86rem; color: var(--ink-soft); line-height: 1.7; white-space: pre-wrap; }

/* ── 推理轨迹 ── */
.trace-panel { max-height: 520px; overflow-y: auto; }
.react-round {
    background: var(--canvas); border-left: 4px solid var(--blue);
    border-radius: 8px; padding: 0; margin: 6px 0; overflow: hidden;
}
.react-round .round-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: rgba(23, 105, 212, 0.04); }
.react-round .round-label { font-size: 0.7rem; font-weight: 700; color: var(--blue); text-transform: uppercase; letter-spacing: 0.05em; }
.react-round .tool-badge {
    display: inline-flex; align-items: center; gap: 4px;
    background: var(--blue); color: white; font-size: 0.68rem;
    font-weight: 600; padding: 2px 10px; border-radius: 10px;
}
.react-round .thought-text { color: #475569; font-style: italic; padding: 6px 12px; font-size: 0.83rem; line-height: 1.45; }
.react-round .action-text {
    color: var(--blue-deep); font-weight: 600; font-family: 'Consolas', monospace;
    font-size: 0.76rem; padding: 4px 12px;
    background: rgba(23, 105, 212, 0.03); word-break: break-all;
}
.react-round .obs-block {
    color: var(--ink-soft); font-size: 0.78rem; line-height: 1.5;
    background: white; padding: 8px 12px;
    max-height: 220px; overflow-y: auto; white-space: pre-wrap;
    font-family: 'Consolas', monospace; border-top: 1px solid var(--line);
}

/* ── 工具调用实时结果面板 ── */
.tool-results-panel { max-height: 340px; overflow: hidden; }
.tool-results-panel .result-card { margin: 0; height: 340px; display: flex; flex-direction: column; }
.tool-results-panel .card-title { flex-shrink: 0; }
.tool-steps-list { flex: 1; overflow-y: auto; min-height: 0; }
.tool-step { display: flex; align-items: flex-start; gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--line-soft); font-size: 0.8rem; transition: background 0.15s; }
.tool-step:hover { background: var(--canvas); }
.tool-step:last-child { border-bottom: none; }
.tool-step-icon { flex-shrink: 0; font-size: 0.9rem; }
.tool-step-name { flex-shrink: 0; font-weight: 700; color: var(--blue); min-width: 52px; font-size: 0.75rem; }
.tool-step-preview {
    color: #475569; line-height: 1.45; font-size: 0.78rem;
    overflow: hidden; text-overflow: ellipsis;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    word-break: break-all;
}

/* ── 历史对话面板 ── */
.history-panel { max-height: 460px; overflow: hidden; }
.history-panel .empty-state { margin: 0; }
.history-list { display: flex; flex-direction: column; gap: 12px; max-height: 420px; overflow-y: auto; padding-right: 4px; }
.history-item { background: white; border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(23, 33, 43, 0.05); }
.history-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding-bottom: 8px; margin-bottom: 10px; border-bottom: 1px solid var(--line); }
.history-title { font-size: 0.9rem; font-weight: 800; color: var(--ink); }
.history-block { margin: 8px 0; }
.history-label { font-size: 0.72rem; font-weight: 800; color: var(--muted); margin-bottom: 4px; }
.history-text { background: var(--canvas); border: 1px solid var(--line-soft); border-radius: 10px; padding: 8px 10px; color: var(--ink-soft); font-size: 0.82rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.history-answer { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 8px 10px; color: #166534; font-size: 0.82rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word; max-height: 180px; overflow-y: auto; }

/* ── 硬件联动状态卡片 ── */
.hw-card { padding: 10px 12px; border-radius: 9px; font-size: 0.88rem; line-height: 1.6; }
.hw-ok { background: #ecfdf5; color: #047857; }
.hw-danger { background: #fef2f2; color: #b91c1c; }
.hw-idle { background: var(--canvas); color: var(--muted); }

/* ── 滚动条 ── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted-weak); }

/* ── 响应式 ── */
@media (max-width: 1280px) {
    .summary-grid { grid-template-columns: repeat(2, 1fr); }
    .kpi-grid { grid-template-columns: repeat(2, 1fr); }
    .analysis-grid { grid-template-columns: 1fr; }
    .header-bar h1 { font-size: 1.1rem; }
}
@media (max-width: 1080px) {
    .workspace { grid-template-columns: 310px minmax(0, 1fr); }
}
@media (max-width: 900px) {
    .desktop-top-grid { flex-direction: column !important; }
    .summary-grid { grid-template-columns: repeat(2, 1fr); }
    .mini-card-row { flex-direction: column !important; }
    .bottom-grid { grid-template-columns: 1fr; }
    .table-surface table { min-width: 640px; font-size: 12px; }
}
@media (max-width: 780px) {
    .topbar-meta { display: none; }
    .safety-list { grid-template-columns: 1fr; }
    .kpi-grid { grid-template-columns: 1fr; }
}
"""
