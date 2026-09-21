# 原版 UI 前端(参考资料)

来源:`D:\Desktop\model training`(硬件展示版接的最新前端),Flask + Jinja2 + SSE 架构,
2026-08-03 由 Gradio 迁移而来,风格为 Figma 数据驾驶舱。

本目录**仅作界面设计参考**,不含模型、Arduino 硬件桥接与训练产物。

| 文件 | 用途 |
|---|---|
| `app.py` | Flask 入口(页面路由 / SSE 流式 / 硬件联动) |
| `templates/index.html` | 页面布局(Jinja2 模板) |
| `static/app.css` | 样式 / 主题 |
| `static/app.js` | 交互 + SSE 流式 |
| `static/vessel-ocean-background.jpg` | 背景图 |
| `PLAN_FIGMA_MIGRATION.md` | Gradio→Flask 迁移设计说明 |
| `ui/theme.py` `ui/components.py` `ui/head.py` | 旧 Gradio 组件(主题/HTML 片段,作样式参考) |

新 Ship-Agent 界面(C 负责)可复用 `templates/` + `static/` 的布局与样式,后端契约见
`core/intent_translator.py`(adjustment_options / boundary_diagnostics)与 `core/report.py`(报告结构)。
