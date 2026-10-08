"""Lightweight Charts 繪圖模組:輸出單一 HTML 檔(圖表庫由 CDN 載入,需網路)。"""

import json
from pathlib import Path


def render(candles, markers, equity, out_html="chart.html", title="我的交易策略"):
    """產生單一 HTML 圖表檔(K 線 + 進出場標記 + 權益曲線;JS 庫走 CDN,離線不渲染)。"""
    lw_markers = [
        {
            "time": m["time"],
            "position": "belowBar" if m["side"] == "buy" else "aboveBar",
            "color": "#26a69a" if m["side"] == "buy" else "#ef5350",
            "shape": "arrowUp" if m["side"] == "buy" else "arrowDown",
            "text": m.get("text", m["side"]),
        }
        for m in markers
    ]
    payload = {
        "candles": candles,
        "markers": lw_markers,
        "equity": [{"time": e["time"], "value": e["value"]} for e in equity],
        "title": title,
    }
    html = _TEMPLATE.replace("__PAYLOAD__", _safe_json(payload))
    Path(out_html).write_text(html, encoding="utf-8")
    return out_html


def _safe_json(payload):
    """把資料序列化成可安全嵌入 <script> 的 JSON。

    json.dumps 不會跳脫 < > &,若 title 或標的名含 "</script>" 會提前
    閉合 script 區塊造成 XSS。這裡把這些字元轉成 unicode escape。
    """
    s = json.dumps(payload, ensure_ascii=False)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_TEMPLATE = """<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">
<title>交易策略圖表</title>
<script src=\"https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js\"></script>
<style>body{margin:0;background:#131722;color:#d1d4dc;font-family:system-ui}
h2{padding:12px 16px;margin:0}#c{height:60vh}#e{height:28vh}</style></head>
<body><h2 id=\"t\"></h2><div id=\"c\"></div><div id=\"e\"></div>
<script>const D=__PAYLOAD__;document.getElementById('t').textContent=D.title;
const opt={layout:{background:{color:'#131722'},textColor:'#d1d4dc'},
grid:{vertLines:{color:'#1e222d'},horzLines:{color:'#1e222d'}}};
const c=LightweightCharts.createChart(document.getElementById('c'),opt);
const s=c.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',
borderVisible:false,wickUpColor:'#26a69a',wickDownColor:'#ef5350'});
s.setData(D.candles);s.setMarkers(D.markers);
const e=LightweightCharts.createChart(document.getElementById('e'),opt);
const l=e.addAreaSeries({lineColor:'#2962ff',topColor:'rgba(41,98,255,.4)',
bottomColor:'rgba(41,98,255,0)'});l.setData(D.equity);
new ResizeObserver(()=>{c.applyOptions({width:innerWidth});
e.applyOptions({width:innerWidth})}).observe(document.body);
c.applyOptions({width:innerWidth});e.applyOptions({width:innerWidth});</script>
</body></html>"""
