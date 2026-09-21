# Figma 前端迁移计划

V2 标签: `cfe2322` (已封存，可随时回溯)

## 一、目录结构变化对比

### 迁移前（当前 Gradio 架构）
```
model training/
├── app.py              ← Gradio Blocks + launch()，需替换
├── ui/
│   ├── theme.py        ← 自定义 CSS/Gradio 主题，废弃
│   ├── components.py   ← Python 拼 HTML，废弃
│   ├── callbacks.py    ← Gradio 回调，硬件联动逻辑需提取
│   ├── parsers.py      ← 数据解析，保留不动
│   ├── hardware_callbacks.py ← 硬件连接回调，逻辑需提取
│   ├── head.py         ← JS 注入，废弃
│   └── voice_api.py    ← 语音 API，保留不动
├── hardware_bridge/    ← Arduino 串口，保留不动
├── deterministic_loop.py ← 推理引擎，保留不动
├── react_loop.py       ← ReAct 循环，保留不动
├── xfyun_asr.py        ← 科大讯飞 ASR，保留不动
├── templates/          ← 新增：Jinja2 模板
└── static/             ← 新增：CSS/JS/图片
```

### 迁移后（Flask 架构）
```
model training/
├── app.py              ← Flask + Jinja2 + SSE，重写
├── bridge.py           ← 新增：推理桥接层（连接 Flask ↔ deterministic_loop.py）
├── arduino_bridge.py   ← 新增：Arduino 硬件桥接（从 callbacks.py + hardware_callbacks.py 提取）
├── ui/
│   └── parsers.py      ← 保留不动
├── hardware_bridge/    ← 保留不动（controller.py, serial_manager.py, output_adapter.py, protocol.py, schemas.py）
├── deterministic_loop.py ← 保留不动
├── react_loop.py       ← 保留不动
├── model_narrator.py   ← 保留不动
├── tools/              ← 保留不动
├── xfyun_asr.py        ← 保留不动
├── templates/
│   └── index.html      ← 直接从 figma 复制，小改
├── static/
│   ├── app.css         ← 直接从 figma 复制
│   ├── app.js          ← 直接从 figma 复制，微调 API 路径
│   └── vessel-ocean-background.jpg ← 直接从 figma 复制
└── requirements_ui.txt ← 更新：flask 替代 gradio
```

## 二、文件分类清单

### 直接照搬（Copy as-is）
| 来源 | 目标 | 说明 |
|------|------|------|
| `figma/static/app.css` | `static/app.css` | 完整 CSS 设计系统，77 行纯 CSS |
| `figma/static/vessel-ocean-background.jpg` | `static/vessel-ocean-background.jpg` | 海洋背景图 |
| `figma/static/app.js` | `static/app.js` | 前端 JS，384 行，几乎不需要改 |
| `figma/templates/index.html` | `templates/index.html` | 页面结构，只需改标题和品牌名 |
| `figma/xfyun_asr.py` | `xfyun_asr.py` | 已复制，保留不动 |

### 需要适配（Adapt）
| 来源 | 目标 | 改什么 |
|------|------|--------|
| `figma/app.py` | `app.py` (重写) | Flask 后端 + 路由。语音 API 逻辑不动， `/api/infer` 从引用 `bridge.stream_inference` 改为我们自己的桥接 |
| `figma/bridge.py` | `bridge.py` (重写) | 把对 `green_navigation_ui-2/react_loop.py` 的依赖改为 `deterministic_loop.py`。`load_routes/load_route_details` 改用 `tools/route_data.py` 的 `WAYPOINTS`。`_dashboard_data` 保持相同解析器调用（parsers.py） |
| 当前 `ui/callbacks.py` | 提取到 `bridge.py` + `arduino_bridge.py` | 推理流程（`run_inference` 的逻辑）转化为 SSE generator；Arduino 控制（`BRIDGE` 单例操作）提取为独立模块 |

### 废弃（可删除）
| 文件 | 原因 |
|------|------|
| `ui/theme.py` | Gradio 专属 CSS + 主题 |
| `ui/components.py` | Python 拼 HTML，不再需要 |
| `ui/callbacks.py` | Gradio 回调，逻辑已提取 |
| `ui/hardware_callbacks.py` | Gradio 回调，逻辑已提取 |
| `ui/head.py` | Gradio head 注入 JS |
| `ui/voice_api.py` | 语音路由已合并到 Flask app.py |

### 保留不动
| 文件 | 说明 |
|------|------|
| `hardware_bridge/` 整个目录 | controller.py, serial_manager.py, output_adapter.py, protocol.py, schemas.py |
| `deterministic_loop.py` | 固定五步推理引擎 |
| `react_loop.py` | ReAct 推理循环 |
| `model_narrator.py` | ship-qwen 讲解 |
| `ui/parsers.py` | 数据解析（parse_route_overview 等） |
| `tools/` | 航线数据、工具函数 |
| `xfyun_asr.py` | 语音识别 |

## 三、Flask 路由设计

| 方法 | 路径 | 功能 | 来源 |
|------|------|------|------|
| GET | `/` | 渲染 index.html | 照搬 figma |
| GET | `/api/routes` | 返回航线列表 | 适配：用 `tools/route_data.py` 的 WAYPOINTS |
| GET | `/api/route-details` | 返回航线节点详情 | 适配：用 WAYPOINTS 构建 |
| GET | `/api/health` | 健康检查 | 照搬 figma |
| POST | `/api/infer` | SSE 流式推理 | 适配：调用 `bridge.stream_inference` |
| POST | `/api/voice/start` | 语音开始 | 照搬 figma（已复制） |
| POST | `/api/voice/chunk` | 语音中间数据 | 照搬 figma |
| POST | `/api/voice/finish` | 语音结束 | 照搬 figma |
| GET | `/api/hardware/status` | Arduino 连接状态 | 新增 |
| POST | `/api/hardware/connect` | 连接 Arduino | 新增 |
| POST | `/api/hardware/disconnect` | 断开 Arduino | 新增 |
| POST | `/api/hardware/lock` | 锁定旋钮读数 | 新增 |

## 四、bridge.py 核心改造

Figma 的 bridge.py 调用 `react_loop.react_reason_stream(message)`，我们的推理引擎在 `deterministic_loop.deterministic_reason_stream(message, structured_context)`。

改动：
1. 导入从 `react_loop` 改为 `deterministic_loop`
2. `stream_inference` 内部调用 `deterministic_reason_stream`，但先构建 `structured_context`
3. 增加硬件 snapshot 获取逻辑（调用 `arduino_bridge.ensure_hardware_snapshot`）
4. 增加安全输出适配器调用（`hardware_bridge.output_adapter.decision_from_tools` + `encode_plan`）
5. 增加硬件下发逻辑（`BRIDGE.send_step`, `BRIDGE.send_decision_with_ack`）
6. 输出格式保持与 figma 相同的 SSE event 结构（`phase/round/thought/tool/observation/final_answer/status/complete/dashboard`）

## 五、arduino_bridge.py 设计

从 `ui/callbacks.py` 和 `ui/hardware_callbacks.py` 提取 Arduino 控制逻辑：

```python
# 从 ui/hardware_callbacks.py 提取
BRIDGE = BridgeController()  # 单例

def ensure_hardware_snapshot(timeout=6.0) -> HardwareSnapshot: ...
def connect_hardware(port: str) -> str: ...
def disconnect_hardware() -> str: ...
def lock_hardware_snapshot() -> str: ...

# 状态查询
def get_hardware_status() -> dict: ...  # 返回 {connected, seq, water_ms, wind_ms, angle_deg, locked, last_ack, last_error, last_sent}
```

## 六、需要修改的 JS（app.js）

| 行数 | 改动 | 原因 |
|------|------|------|
| 全局 | API 路径不变（/api/*） | Flask 路由与 figma 一致 |
| `loadRoutes()` | 数据格式适配 | WAYPOINTS 格式可能与 `all_ports_v7.json` 不同 |
| `resolveRouteNodes()` | 距离数据格式 | 同上 |
| 硬件状态轮询 | 新增 | 需要定时 GET `/api/hardware/status` 更新连接状态和旋钮值 |
| 硬件连接按钮 | 新增 | HTML 模板需增加硬件控制区 |

## 七、HTML 模板改动（index.html）

| 改动 | 说明 |
|------|------|
| 标题 `h1` | 从"船舶航速优化..."改为"绿航智算 · 内河船舶航速优化与能效管理智能决策系统" |
| eyebrow | 从 "YUJIAOTOU 001 · LOCAL DECISION CONSOLE" 改为 "YUJIAOTOU 001 · FIXED 5-STEP TOOLCHAIN · ARDUINO LINK" |
| 新增硬件控制区 | 左侧面板底部增加：COM口选择、连接/断开按钮、锁定旋钮按钮、状态显示 |
| 快捷示例按钮 | 从"港口A→港口C"改为"平顶山港→周口港（全程）"等具体航线 |

## 八、风险与依赖

| 风险 | 应对 |
|------|------|
| `bridge.py` 的 `react_reason_stream` 接口与 `deterministic_reason_stream` 不完全兼容 | `deterministic_reason_stream` 需要 `structured_context` 参数，需在 bridge 中构建 |
| `tools/route_data.py` 的 WAYPOINTS 结构 vs figma 的 `all_ports_v7.json` 格式不同 | 检查两者差异，适配 `load_routes` 和 `load_route_details` |
| Arduino 调用在异步 Flask 中需线程安全 | `BRIDGE` 已用 `threading.Lock`，保持单线程串口操作 |
| 背景图 `vessel-ocean-background.jpg` 541KB | 答辩环境需确保静态文件可访问 |
| Lucide 图标 CDN 依赖 (`unpkg.com`) | 答辩需上网，或本地化 lucide JS |
| Google Fonts 依赖 | 同上，系统字体 fallback 已配置 |

## 九、实施步骤

按顺序执行，每步完成后验证。

### 第一步：环境准备
- `pip install flask`（Gradio 保留不动，无需卸载）
- 创建 `templates/` 和 `static/` 目录
- 从 figma 复制 `static/app.css`, `static/app.js`, `static/vessel-ocean-background.jpg`
- 从 figma 复制 `templates/index.html` 并做标题/品牌名修改

### 第二步：创建 bridge.py
- 写入推理桥接层，调用 `deterministic_loop.deterministic_reason_stream`
- 集成 `ui/parsers.py` 的解析函数
- 集成 `hardware_bridge.output_adapter` 的安全校验和下发
- 写入 `/api/routes` 和 `/api/route-details` 的数据适配

### 第三步：创建 arduino_bridge.py
- 从 `ui/callbacks.py` + `ui/hardware_callbacks.py` 提取 BRIDGE 操作
- 封装为简单函数接口

### 第四步：重写 app.py
- Flask 应用 + 六个核心路由（index, routes, route-details, health, infer, hardware/*）
- 语音 API 三路由（start/chunk/finish）
- 静态文件服务（Flask 内置）

### 第五步：改装 index.html 和 app.js
- 标题/品牌名修改
- 快捷示例改为真实航线
- 新增硬件控制区 HTML
- app.js 新增硬件状态轮询和控制逻辑

### 第六步：集成测试
- 启动 `python app.py --port 7860`
- 验证页面渲染、推理流程、SSE 实时更新
- 接 Arduino 验证硬件联动

### 第七步：清理
- 删除 `ui/theme.py`, `ui/components.py`, `ui/callbacks.py`, `ui/hardware_callbacks.py`, `ui/head.py`
- 更新 `requirements_ui.txt`（移除 gradio，添加 flask）
- git commit
