"""主程式 —— 預設用紙上模擬跑一遍策略,並產生圖表。

安全設計:
  - ALLOW_LIVE_TRADING 預設為 False。即使你接了真實券商,
    也要明確改成 True 並通過安全閘門,才會真的下真錢訂單。
"""

import csv
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import yaml

from strategy import Strategy
from broker_setup import build_broker
from data_feed import load_history
from charting import render
from broker_lib import Order, OrderSide, BrokerAdapter

# ── 真實下單總開關(預設關閉,保護你的錢)──
ALLOW_LIVE_TRADING = False


def _force_utf8_stdout() -> None:
    """讓獨立腳架在 Windows 非 UTF-8 管線也能輸出中文。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


DEFAULT_CONFIG = {
    "market": 'crypto',
    "symbols": ['BTCUSDT', 'ETHUSDT'],
    "broker": "paper",
    "data": {"source": "demo", "interval": "1d", "bars": 120},
    "strategy": {"fast_ma": 5, "slow_ma": 20},
    "paper": {"starting_cash": 10_000, "fee_rate": 0.001, "slippage": 0.0005},
    "risk": {"stop_loss_pct": 0.05, "take_profit_pct": 0.15,
              "max_position_pct": 0.2},
    # 缺少設定檔或解析失敗時,明確退回未驗證且禁止真實下單。
    "anti_gambling": {
        "stage_code": "unverified",
        "allow_live_trading": False,
    },
}


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        # 沒有 config.yaml 時用內建預設(紙上模擬)
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        # 設定無法讀取或 YAML 解析失敗時絕不猜測,退回安全預設。
        print(
            f"⛔ {path} 無法安全解析({type(exc).__name__}),"
            "改用禁止真實下單的內建預設。"
        )
        return dict(DEFAULT_CONFIG)
    # 空檔或格式錯誤時 safe_load 回 None / 非 dict —— 直接用會 AttributeError。
    # 誠實退回內建預設(紙上模擬)並提醒,而不是丟 traceback。
    if not isinstance(data, dict):
        print(f"⚠️ {path} 是空的或格式不對,改用內建預設(紙上模擬)。")
        return dict(DEFAULT_CONFIG)
    return data


def maybe_enable_live(broker: BrokerAdapter, config: dict) -> None:
    """若使用者明確開啟真實下單,解除安全閘門;否則維持封鎖。

    所有彼此獨立的閘門都通過才會放行:
      1. main.py 的 ALLOW_LIVE_TRADING 常數(要手動改成 True)
      2. config.yaml 的 risk.i_have_read_disclaimer 設為 true
      3. anti_gambling.allow_live_trading 必須是布林值 true
      4. anti_gambling.stage_code 必須精確等於 tiny_live_validation
      5. 券商本身的 confirm_live_trading 雙重確認
    缺欄位、型別不符或其他階段一律 fail closed。
    """
    if not getattr(broker, "is_live", False):
        return  # 紙上模擬,無需解鎖

    if not ALLOW_LIVE_TRADING:
        raise SystemExit(
            "⛔ 偵測到真實券商,但 ALLOW_LIVE_TRADING 為 False。\n"
            "   這是保護你的錢。確認策略已驗證、願意自負風險後,\n"
            "   再把 main.py 的 ALLOW_LIVE_TRADING 改成 True。"
        )

    risk = config.get("risk") if isinstance(config, dict) else None
    if not isinstance(risk, dict) or risk.get("i_have_read_disclaimer") is not True:
        raise SystemExit(
            "⛔ 真實下單的第二道閘門未解除。\n"
            "   請先閱讀免責聲明,並在 config.yaml 的 risk 區塊加上:\n"
            "       i_have_read_disclaimer: true\n"
            "   兩道閘門刻意分開,確保你不是只改了一個地方就誤觸真錢下單。"
        )

    anti_gambling = config.get("anti_gambling")
    if not isinstance(anti_gambling, dict):
        raise SystemExit(
            "⛔ 缺少有效的 anti_gambling 安全設定,真實下單維持封鎖。\n"
            "   請重新執行含完整交易分析的 scaffold,不要手動猜測階段。"
        )

    if anti_gambling.get("allow_live_trading") is not True:
        raise SystemExit(
            "⛔ 交易分析尚未允許真實下單。\n"
            "   anti_gambling.allow_live_trading 必須是布林值 true;"
            "缺少、false 或字串值一律封鎖。"
        )

    if anti_gambling.get("stage_code") != "tiny_live_validation":
        raise SystemExit(
            "⛔ 目前交易階段不允許真實下單。\n"
            "   只有 stage_code: tiny_live_validation 才可能放行;"
            "缺少或其他階段一律維持紙上模擬。"
        )

    broker.confirm_live_trading(i_understand_the_risk=True)


def calculate_order_quantity(budget, price, market: str):
    """按市場保守計算數量；永遠不把不足一單位強制放大成 1。"""
    try:
        budget_d = Decimal(str(budget))
        price_d = Decimal(str(price))
    except (InvalidOperation, ValueError):
        return 0
    if not budget_d.is_finite() or not price_d.is_finite():
        return 0
    if budget_d <= 0 or price_d <= 0:
        return 0
    raw = budget_d / price_d
    if str(market).lower() == "crypto":
        # 先保守截到 8 位；交易所 adapter 仍會按即時 symbol 規格再次驗證。
        return float(raw.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))
    return int(raw)


def reject_live_historical_replay(broker: BrokerAdapter) -> None:
    """內建 run() 是歷史/示範 replay，絕不允許它呼叫真實券商。"""
    if getattr(broker, "is_live", False):
        raise SystemExit(
            "⛔ main.py 的 run() 會重播歷史／示範 K 線，禁止連接真實券商。\n"
            "   否則歷史訊號可能被一次送成多張真單。請另寫只處理最新已完成 K 線、\n"
            "   且資料來源可驗證為即時的 live runner；不要解除這道閘門。"
        )


def _fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def export_trades(trades: list, path: str = "paper_trades.csv") -> str:
    """把已平倉的紙上交易存成反詐投資王 analyze 讀得懂的格式。"""
    fields = ["代號", "方向", "進場時間", "出場時間", "進場價", "出場價",
              "數量", "手續費", "損益", "損益幣別", "策略"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)
    return path


def run():
    _force_utf8_stdout()
    config = load_config()
    broker = build_broker(config)
    maybe_enable_live(broker, config)
    reject_live_historical_replay(broker)
    broker.connect()  # live broker 已在上一步退出；只有 paper 會走到這裡

    strategy = Strategy(config)
    symbols = config.get("symbols", ['BTCUSDT', 'ETHUSDT'])
    market = str(config.get("market", 'crypto'))
    risk = config.get("risk", {})
    max_pct = risk.get("max_position_pct", 0.2)

    all_markers = []
    equity_curve = []
    candles_for_chart = []
    n_fills = 0
    chart_symbol = symbols[-1] if symbols else ""   # 圖表以最後一檔為例

    # 時間為外圈、標的為內圈。若按標的逐檔跑完整段歷史,前面標的的
    # 期末損益會被灌進後面標的的「歷史」權益曲線(時間穿越),圖會說謊。
    data_cfg = config.get("data") or {}
    source = str(data_cfg.get("source", "demo"))
    interval = str(data_cfg.get("interval", "1d"))
    bars = int(data_cfg.get("bars", 120))
    if source == "demo":
        print("⚠️ 使用內建示範資料(不是真實行情),績效數字沒有任何意義。\n"
              "   要用真實 K 線,把 config.yaml 的 data.source 改成 binance。")
    histories = {sym: load_history(sym, n=bars, source=source, interval=interval)
                 for sym in symbols}
    open_trades = {}     # symbol -> 進場資訊
    closed_trades = []   # 已平倉交易(給 analyze 用)
    strategy.prepare(histories)
    n_bars = min((len(h) for h in histories.values()), default=0)
    if chart_symbol:
        candles_for_chart = histories[chart_symbol][:n_bars]

    for i in range(n_bars):
        for symbol in symbols:
            history = histories[symbol]
            window = history[: i + 1]
            bar = window[-1]
            # 紙上模擬需要餵價
            if hasattr(broker, "set_price"):
                broker.set_price(symbol, bar["close"])

            positions = {p.symbol: p for p in broker.get_positions()}
            pos = positions.get(symbol)
            sig = strategy.on_bar(symbol, window, pos)

            if sig.action == "buy" and pos is None:
                acct = broker.get_account()
                budget = acct.equity * max_pct
                qty = calculate_order_quantity(budget, bar["close"], market)
                if qty <= 0:
                    continue  # 預算不足一單位就不下單，不可偷偷放大部位
                r = broker.place_order(Order(symbol, OrderSide.BUY, qty,
                                             client_tag=sig.reason))
                if r.ok:
                    n_fills += 1
                    open_trades[symbol] = {"time": bar["time"], "price": r.avg_price,
                                           "qty": r.filled_quantity,
                                           "fee": r.raw.get("fee", 0.0),
                                           "reason": sig.reason}
                # 圖表標記只收「被繪製那一檔」的訊號 —— 其他標的的標記
                # 疊在別檔的 K 線上會畫錯位置,誤導判讀。
                if r.ok and symbol == chart_symbol:
                    all_markers.append({"time": bar["time"], "price": bar["low"],
                                         "side": "buy", "text": sig.reason or "買"})
            elif sig.action == "sell" and pos is not None:
                r = broker.place_order(Order(symbol, OrderSide.SELL, abs(pos.quantity),
                                             client_tag=sig.reason))
                if r.ok:
                    n_fills += 1
                    entry = open_trades.pop(symbol, None)
                    if entry is not None:
                        fee = entry["fee"] + r.raw.get("fee", 0.0)
                        pnl = (r.avg_price - entry["price"]) * r.filled_quantity - fee
                        closed_trades.append({
                            "代號": symbol, "方向": "買",
                            "進場時間": _fmt_time(entry["time"]),
                            "出場時間": _fmt_time(bar["time"]),
                            "進場價": f"{entry['price']:.8g}",
                            "出場價": f"{r.avg_price:.8g}",
                            "數量": f"{r.filled_quantity:.8g}",
                            "手續費": f"{fee:.8f}",
                            "損益": f"{pnl:.8f}",
                            "損益幣別": "USDT",
                            "策略": f"{entry['reason']} → {sig.reason}",
                        })
                if r.ok and symbol == chart_symbol:
                    all_markers.append({"time": bar["time"], "price": bar["high"],
                                         "side": "sell", "text": sig.reason or "賣"})

        # 每個時間點取樣一次「帳戶總權益」(時間軸對齊圖表主標的)——
        # 在內圈取樣會把同一時間戳寫入多次,或漏掉其他標的的損益貢獻
        if chart_symbol:
            equity_curve.append({"time": histories[chart_symbol][i]["time"],
                                  "value": broker.get_account().equity})

    acct = broker.get_account()
    print("=" * 50)
    # 用實際 broker 實例的名稱:config 寫 shioaji 但 adapter 還沒解註解時,
    # 實際跑的是 PaperBroker —— 印 config 值會誤導使用者以為單已送到券商
    print(f"  策略執行完畢（{getattr(broker, 'name', '?')} 模式）")
    print(f"  最終權益: {acct.equity:,.2f}")
    print(f"  成交筆數: {n_fills}")
    print(f"  已平倉交易: {len(closed_trades)} 筆(未平倉 {len(open_trades)} 筆不計入)")
    print("=" * 50)
    ai = getattr(strategy, "ai", None)
    if ai is not None:
        print(f"  {ai.summary()}")
        if ai.decisions:
            with open("ai_decisions.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["時間", "代號", "分數", "理由"])
                w.writeheader()
                for d in ai.decisions:
                    w.writerow({"時間": _fmt_time(d["time"]), "代號": d["symbol"],
                                "分數": f"{d['conviction']:+.2f}", "理由": d["thesis"]})
            print("  AI 每次判斷的分數與理由: ai_decisions.csv")
    if closed_trades:
        path = export_trades(closed_trades)
        print(f"  交易紀錄已存成: {path}")
        print("  下一步:交給反詐投資王檢驗這套規則是方法還是運氣 ——")
        print(f"    python -m core.cli analyze {path}")
        if source == "demo":
            print("  (目前是示範資料,這份紀錄只用來確認流程,分析結果不代表任何事)")

    out = render(candles_for_chart, all_markers, equity_curve,
                 out_html="chart.html", title='binance_bot')
    print(f"  圖表已產生: {out}")
    if getattr(broker, "is_live", False):
        print("  ⚠️ 這是真實下單模式,以上為真實訂單結果。")


if __name__ == "__main__":
    run()
