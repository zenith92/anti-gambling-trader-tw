"""你的交易策略 —— 目前放的是「範例規則」,不是經過驗證的賺錢方法。

反詐投資王裁決:unknown
(尚未完成含樣本外與風險基準的交易階段分析)

範例規則:均線交叉(預設 5 / 20 根 K 線,可在 config.yaml 的 strategy 區塊調整)
  - 進場:快線「由下往上穿過」慢線(黃金交叉)那一根才買,不是只要在上方就買
  - 出場:停損 / 停利,或快線「由上往下穿過」慢線(死亡交叉)
  - 只做多,不放空、不加槓桿

均線交叉是最廣為人知的規則之一,在手續費與滑價之後通常沒有優勢。
它放在這裡是讓你看懂流程,請換成你自己寫得出、說得清楚的規則,
再用紙上模擬跑出完整交易紀錄,交給 `analyze` 檢驗。
寫得出規則,才有資格談自動化。
"""

from dataclasses import dataclass


@dataclass
class Signal:
    """策略對單一標的、單一時間點的決策。"""
    action: str          # "buy" | "sell" | "hold"
    reason: str = ""     # 進出場理由(會成為交易 tag,方便日後分析哪套邏輯有效)


def _sma(values: list, n: int) -> float:
    return sum(values[-n:]) / n


class Strategy:
    def __init__(self, config: dict):
        self.cfg = config
        self.stop_loss = config.get("risk", {}).get("stop_loss_pct", 0.05)
        self.take_profit = config.get("risk", {}).get("take_profit_pct", 0.15)
        params = config.get("strategy") or {}
        self.fast = int(params.get("fast_ma", 5))
        self.slow = int(params.get("slow_ma", 20))
        if not (1 <= self.fast < self.slow):
            raise ValueError("strategy.fast_ma 必須 >= 1 且小於 strategy.slow_ma")

    def on_bar(self, symbol: str, history: list, position) -> Signal:
        """每根 K 線呼叫一次,回傳決策。

        Args:
            symbol:   標的代號
            history:  到目前為止的 K 線清單(dict: time/open/high/low/close/volume),
                      只含已收盤的 K 線,最後一根就是「現在」—— 不可偷看未來
            position: 目前持倉(None 表示空手)
        """
        # 某些現貨 balance API 只有數量、沒有成本價。未知成本時用示範 K 線
        # 自動停損/停利可能立刻賣掉真實持倉，因此一律 hold，等使用者補可靠成本。
        if position is not None and not getattr(position, "cost_basis_known", True):
            return Signal("hold", "成本基準未知，禁止自動停損／停利")

        # ── 出場:停損 / 停利(預設邏輯,建議保留)──
        if position is not None and history:
            price = history[-1]["close"]
            entry = position.avg_price
            if price <= entry * (1 - self.stop_loss):
                return Signal("sell", "觸發停損")
            if price >= entry * (1 + self.take_profit):
                return Signal("sell", "觸發停利")

        # 需要「前一根」的均線才能判斷是否剛好穿越
        if len(history) < self.slow + 1:
            return Signal("hold")

        closes = [h["close"] for h in history]
        fast_now, slow_now = _sma(closes, self.fast), _sma(closes, self.slow)
        fast_prev, slow_prev = _sma(closes[:-1], self.fast), _sma(closes[:-1], self.slow)
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        # ── 出場:死亡交叉 ──
        if position is not None and crossed_down:
            return Signal("sell", f"MA{self.fast}下穿MA{self.slow}")

        # ── 進場:黃金交叉 ──
        if position is None and crossed_up:
            return Signal("buy", f"MA{self.fast}上穿MA{self.slow}")

        return Signal("hold")
