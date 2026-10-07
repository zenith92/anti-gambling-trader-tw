"""資料來源:回測用歷史 K 線 / 即時報價的統一介面。

兩種來源(在 config.yaml 的 data.source 選擇):
  - demo:    內建的確定性「示範資料產生器」,離線可跑,但**不是真實行情**,
             跑出來的績效沒有任何意義,只用來確認程式能動。
  - binance: Binance 公開行情 API(只讀、不需要 API key、不會下單)。
             抓取失敗會直接報錯,不會偷偷改用示範資料 —— 避免你把假資料的
             回測結果誤當真。
"""

import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request

# Binance 官方的「純行情」端點,不需要金鑰;被擋時可改 api.binance.com
BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
_MAX_PER_REQUEST = 1000
_CACHE_DIR = "data_cache"


def load_history(symbol: str, n: int = 120, source: str = "demo",
                 interval: str = "1d") -> list:
    """回傳最近 n 根「已收盤」K 線(time 為 UTC 秒)。"""
    if source == "demo":
        return _demo_history(n)
    if source == "binance":
        return _binance_history(symbol, n, interval)
    raise ValueError(f"未知的資料來源 data.source={source!r}(可選 demo / binance)")


def _demo_history(n: int) -> list:
    """示範用,確定性、可重現。不是真實行情。"""
    candles = []
    base_ts = 1_700_000_000
    price = 100.0
    for i in range(n):
        drift = math.sin(i / 7.0) * 5 + i * 0.12
        o = price
        c = 100 + drift
        h = max(o, c) + abs(math.sin(i)) * 1.2 + 0.4
        low = min(o, c) - abs(math.cos(i)) * 1.2 - 0.4
        candles.append({
            "time": base_ts + i * 86400,
            "open": round(o, 2), "high": round(h, 2),
            "low": round(low, 2), "close": round(c, 2),
            "volume": 1000 + (i % 9) * 100,
        })
        price = c
    return candles


def _fetch_klines(symbol: str, interval: str, limit: int, end_time_ms=None) -> list:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    url = f"{BINANCE_KLINES_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "binance_bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _binance_history(symbol: str, n: int, interval: str) -> list:
    if n <= 0:
        return []
    now_ms = int(time.time() * 1000)
    rows: list = []
    end_time = None
    try:
        # 由新往舊分頁抓,直到湊滿 n 根(多抓 1 根,用來丟掉尚未收盤的那根)
        while len(rows) < n + 1:
            want = min(_MAX_PER_REQUEST, n + 1 - len(rows))
            batch = _fetch_klines(symbol, interval, want, end_time)
            if not batch:
                break
            rows = batch + rows
            end_time = batch[0][0] - 1
            if len(batch) < want:
                break   # 已經到上市第一根
    except OSError as exc:   # URLError / HTTPError / timeout 都是 OSError
        raise SystemExit(
            f"⛔ 無法從 Binance 取得 {symbol} 的 K 線:{exc}\n"
            "   請確認網路可連到 data-api.binance.vision,或把 config.yaml 的\n"
            "   data.source 改回 demo(只能測程式,績效無意義)。"
        ) from exc

    candles = []
    for k in rows:
        open_ms, o, h, low, c, vol, close_ms = k[0], k[1], k[2], k[3], k[4], k[5], k[6]
        if close_ms >= now_ms:
            continue   # 尚未收盤的 K 線會變動,用它做決策等於偷看未來
        candles.append({
            "time": open_ms // 1000,
            "open": float(o), "high": float(h), "low": float(low),
            "close": float(c), "volume": float(vol),
        })
    candles = candles[-n:]
    _save_cache(symbol, interval, candles)
    return candles


def _save_cache(symbol: str, interval: str, candles: list) -> None:
    """把抓到的 K 線另存一份 CSV,方便你用 Excel 檢查資料是否正確。"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = os.path.join(_CACHE_DIR, f"{symbol}_{interval}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["time", "open", "high", "low", "close", "volume"])
            w.writeheader()
            w.writerows(candles)
    except OSError:
        pass   # 快取只是方便檢查,寫不進去不影響回測
