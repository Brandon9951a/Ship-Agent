import json
from pathlib import Path

from ui.web_server import ROOT, Handler


def test_frontend_assets_exist_and_have_core_surfaces():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "app.js").read_text(encoding="utf-8")
    for label in ("任务描述", "航线与分段方案", "推荐航速与能耗", "安全校验", "下一步"):
        assert label in html
    assert "@media" in css
    assert "/api/run" in js

