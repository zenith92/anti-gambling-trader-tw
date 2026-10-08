"""每日前向紙上模擬(forward paper trading)—— 每天排程跑一次,不碰真錢。

和 main.py 的回測不同:回測是「事後拿整段歷史套規則」,這支程式只處理
**建立帳戶之後才收盤**的 K 線,所以每一筆交易都是規則事先寫好、無法偷看未來的
真正樣本外紀錄。累積夠多筆之後,交給反詐投資王檢驗:

    python -m core.cli analyze my_bots/binance_bot/forward_trades.csv

設計:
  - 帳戶狀態(現金、持倉、處理到哪根 K 線)存在 paper_state.json,每次執行接續上次。
  - 第一次執行只建立帳戶,不交易;之後每次只處理新收盤的 K 線(漏跑幾天會依序補上)。
  - 同一天重跑不會重複交易。
  - 規則或設定一改就拒絕執行:邊看結果邊改規則,樣本就不再是樣本外。
    要換規則,請把舊的 paper_state.json / forward_*.csv 改名保存,重新開始。
  - 只用 broker_lib 的 PaperBroker 記帳,不會建立任何真實券商連線。

排程範例(Binance 日 K 線在台灣時間 08:00 收盤):
  crontab -e   →   10 8 * * * cd /path/to/binance_bot && python daily_paper.py >> daily_paper.log 2>&1
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from broker_lib import Order, OrderSide, PaperBroker, Position
from data_feed import load_history
from main import _fmt_time, _force_utf8_stdout, calculate_order_quantity, load_config
from strategy import Strategy

STATE_VERSION = 1
TRADE_FIELDS = ["代號", "方向", "進場時間", "出場時間", "進場價", "出場價",
                "數量", "手續費", "損益", "損益幣別", "策略"]
LOG_FIELDS = ["K線時間", "代號", "收盤價", "動作", "理由", "AI分數", "AI理由", "帳戶權益"]


def _rules_fingerprint(config: dict) -> str:
    """會影響交易結果的設定都納入;任何一項變動都代表換了一套規則。"""
    keys = ("symbols", "market", "data", "strategy", "ai", "paper", "risk")
    relevant = {k: config.get(k) for k in keys}
    # 抓幾根歷史、API 呼叫上限只影響效能與費用,不影響規則
    if isinstance(relevant.get("data"), dict):
        relevant["data"] = {k: v for k, v in relevant["data"].items() if k != "bars"}
    if isinstance(relevant.get("ai"), dict):
        relevant["ai"] = {k: v for k, v in relevant["ai"].items() if k != "max_calls"}
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # 寫到一半當機也不會留下壞掉的狀態檔


def _append_csv(path: str, fields: list, rows: list) -> None:
    if not rows:
        return
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def _restore_broker(state: dict, paper_cfg: dict) -> PaperBroker:
    broker = PaperBroker(
        cash=state["cash"],
        fee_rate=paper_cfg.get("fee_rate", 0.001),
        slippage=paper_cfg.get("slippage", 0.0005),
        currency="USDT",
    )
    broker.connect()
    # PaperBroker 沒有公開的「載入持倉」介面;同一專案內直接還原上次存下的持倉
    for sym, p in state["positions"].items():
        broker._positions[sym] = Position(sym, p["qty"], p["avg_price"], p["avg_price"])
    return broker


class _Lock:
    """避免排程與手動執行同時跑,把同一根 K 線處理兩次。"""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit(
                f"⛔ {self.path} 存在,可能有另一個 daily_paper.py 正在執行。\n"
                "   確定沒有其他執行中的程式後,刪掉這個檔案再試。"
            )
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        os.remove(self.path)


def run(state_path: str = "paper_state.json", trades_path: str = "forward_trades.csv",
        log_path: str = "forward_log.csv") -> None:
    _force_utf8_stdout()
    config = load_config()
    data_cfg = config.get("data") or {}
    if data_cfg.get("source") != "binance":
        raise SystemExit("⛔ 前向模擬必須用真實行情:請把 config.yaml 的 data.source 設成 binance。")
    interval = str(data_cfg.get("interval", "1d"))
    symbols = list(config.get("symbols") or [])
    market = str(config.get("market", "crypto"))
    paper_cfg = config.get("paper") or {}
    max_pct = (config.get("risk") or {}).get("max_position_pct", 0.2)
    fingerprint = _rules_fingerprint(config)

    strategy = Strategy(config)
    need = max(strategy.slow + 1, strategy.ai.lookback if strategy.ai else 0)
    histories = {s: load_history(s, n=need + 60, source="binance", interval=interval)
                 for s in symbols}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── 第一次執行:只建立帳戶 ──────────────────────────────
    if not os.path.exists(state_path):
        latest = max(h[-1]["time"] for h in histories.values() if h)
        state = {
            "version": STATE_VERSION,
            "created_at": now,
            "rules_fingerprint": fingerprint,
            "cash": float(paper_cfg.get("starting_cash", 10_000)),
            "positions": {},
            "last_bar_time": latest,
        }
        _write_json_atomic(state_path, state)
        print(f"✅ 已建立紙上帳戶 {state_path}(起始 {state['cash']:,.2f} USDT,標的 {', '.join(symbols)})")
        print(f"   目前最新一根已收盤 K 線是 {_fmt_time(latest)} 開盤的那根;從下一根收盤起才會交易,"
              "之前的行情只當歷史背景。")
        print("   請設定每天排程執行這支程式(見檔案開頭說明)。")
        return

    state = _read_json(state_path)
    if state.get("rules_fingerprint") != fingerprint:
        raise SystemExit(
            "⛔ config.yaml 的規則或參數和建立帳戶時不同。\n"
            "   邊跑邊改規則,累積的紀錄就不再是樣本外,統計檢驗會失真。\n"
            f"   要換規則:把 {state_path}、{trades_path}、{log_path} 改名保存,再重新執行以建立新帳戶。"
        )

    with _Lock(state_path + ".lock"):
        last = state["last_bar_time"]
        new_times = sorted({c["time"] for h in histories.values() for c in h if c["time"] > last})
        if not new_times:
            print(f"ℹ️ {now}:沒有新收盤的 K 線(上次處理到 {_fmt_time(last)}),今天不需要動作。")
            return
        if len(new_times) > 1:
            print(f"ℹ️ 有 {len(new_times)} 根 K 線沒處理(可能漏跑),依時間順序補上。")
        if strategy.ai is not None:
            calls = len(new_times) * len(symbols)
            print(f"🤖 本次最多需要 {calls} 次 Claude 呼叫(上限 ai.max_calls={strategy.ai.max_calls})")

        broker = _restore_broker(state, paper_cfg)
        index = {s: {c["time"]: i for i, c in enumerate(h)} for s, h in histories.items()}
        trades, log_rows = [], []

        for t in new_times:
            for sym in symbols:
                i = index[sym].get(t)
                if i is not None:
                    broker.set_price(sym, histories[sym][i]["close"])
            for sym in symbols:
                i = index[sym].get(t)
                if i is None:
                    continue
                window = histories[sym][: i + 1]
                bar = window[-1]
                pos = {p.symbol: p for p in broker.get_positions()}.get(sym)
                n_decisions = len(strategy.ai.decisions) if strategy.ai else 0
                sig = strategy.on_bar(sym, window, pos)
                ai_note = (strategy.ai.decisions[-1] if strategy.ai
                           and len(strategy.ai.decisions) > n_decisions else None)
                action = "觀望"

                if sig.action == "buy" and pos is None:
                    qty = calculate_order_quantity(broker.get_account().equity * max_pct,
                                                   bar["close"], market)
                    r = broker.place_order(Order(sym, OrderSide.BUY, qty, client_tag=sig.reason)) \
                        if qty > 0 else None
                    if r is not None and r.ok:
                        action = "買進"
                        state["positions"][sym] = {
                            "qty": r.filled_quantity, "avg_price": r.avg_price,
                            "entry_time": bar["time"], "entry_fee": r.raw.get("fee", 0.0),
                            "reason": sig.reason,
                        }
                    else:
                        action = "買進失敗"
                elif sig.action == "sell" and pos is not None:
                    r = broker.place_order(Order(sym, OrderSide.SELL, abs(pos.quantity),
                                                 client_tag=sig.reason))
                    if r.ok:
                        action = "賣出"
                        entry = state["positions"].pop(sym)
                        fee = entry["entry_fee"] + r.raw.get("fee", 0.0)
                        pnl = (r.avg_price - entry["avg_price"]) * r.filled_quantity - fee
                        trades.append({
                            "代號": sym, "方向": "買",
                            "進場時間": _fmt_time(entry["entry_time"]),
                            "出場時間": _fmt_time(bar["time"]),
                            "進場價": f"{entry['avg_price']:.8g}",
                            "出場價": f"{r.avg_price:.8g}",
                            "數量": f"{r.filled_quantity:.8g}",
                            "手續費": f"{fee:.8f}",
                            "損益": f"{pnl:.8f}",
                            "損益幣別": "USDT",
                            "策略": f"{entry['reason']} → {sig.reason}",
                        })
                    else:
                        action = "賣出失敗"

                log_rows.append({
                    "K線時間": _fmt_time(bar["time"]), "代號": sym,
                    "收盤價": f"{bar['close']:.8g}", "動作": action, "理由": sig.reason,
                    "AI分數": f"{ai_note['conviction']:+.2f}" if ai_note else "",
                    "AI理由": ai_note["thesis"] if ai_note else "",
                    "帳戶權益": f"{broker.get_account().equity:.2f}",
                })
            state["last_bar_time"] = t

        state["cash"] = broker.get_account().cash
        state["last_run_at"] = now
        # 先寫交易紀錄再寫狀態:中途出錯時寧可重跑被擋,也不要狀態前進但紀錄遺失
        _append_csv(trades_path, TRADE_FIELDS, trades)
        _append_csv(log_path, LOG_FIELDS, log_rows)
        _write_json_atomic(state_path, state)

    acct = broker.get_account()
    print(f"📒 {now} 紙上模擬(不會下真單)處理到 {_fmt_time(state['last_bar_time'])} 收盤")
    for row in log_rows:
        extra = f" | AI {row['AI分數']} {row['AI理由']}" if row["AI分數"] else ""
        print(f"   {row['K線時間']} {row['代號']:<9} 收 {row['收盤價']:<12} {row['動作']} {row['理由']}{extra}")
    held = ", ".join(f"{s} {p['qty']:g} @ {p['avg_price']:.2f}" for s, p in state["positions"].items())
    print(f"   帳戶權益 {acct.equity:,.2f} USDT(現金 {acct.cash:,.2f};持倉 {held or '無'})")
    if trades:
        print(f"   本次平倉 {len(trades)} 筆,已附加到 {trades_path}")
    total = 0
    if os.path.exists(trades_path):
        with open(trades_path, encoding="utf-8-sig") as f:
            total = sum(1 for _ in csv.DictReader(f))
    print(f"   累計已平倉 {total} 筆" + ("(至少 30 筆後再用 analyze 檢驗,現在下結論都太早)"
                                     if total < 30 else ";可以用 analyze 檢驗了"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="每日前向紙上模擬(不會下真單)")
    parser.add_argument("--state", default="paper_state.json", help="帳戶狀態檔")
    parser.add_argument("--trades", default="forward_trades.csv", help="已平倉交易紀錄")
    parser.add_argument("--log", default="forward_log.csv", help="每根 K 線的決策日誌")
    args = parser.parse_args()
    try:
        run(args.state, args.trades, args.log)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 排程執行時留下清楚的錯誤訊息
        print(f"⛔ 執行失敗({type(exc).__name__}):{exc}\n"
              "   狀態檔只在整批 K 線處理成功後才會更新,修正後重跑即可。", file=sys.stderr)
        raise SystemExit(1)
