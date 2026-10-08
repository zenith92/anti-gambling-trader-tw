"""自包含的券商函式庫(由反詐投資王腳架產生)。

本檔內含交易介面與 PaperBroker(紙上模擬),讓本專案不依賴
反詐投資王本體即可獨立執行。真實券商連接器請見 brokers/ 目錄。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"   # 市價單
    LIMIT = "limit"     # 限價單


def _is_finite_number(value: object) -> bool:
    """True only for real int/float finite numbers; bool / NaN / Inf rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


@dataclass
class Order:
    """一張要送出的委託單。"""

    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None      # 限價單必填
    client_tag: Optional[str] = None         # 對應策略標籤,方便日後回頭分析
    client_order_id: Optional[str] = None    # 券商委託識別碼；不可和策略標籤混用

    def validate(self) -> None:
        if not isinstance(self.side, OrderSide):
            raise ValueError("委託方向必須是 OrderSide.BUY 或 OrderSide.SELL")
        if not isinstance(self.order_type, OrderType):
            raise ValueError("委託類型必須是 OrderType.MARKET 或 OrderType.LIMIT")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("委託代號不可空白")
        if not _is_finite_number(self.quantity) or self.quantity <= 0:
            raise ValueError("委託數量必須是有限且大於 0 的數字")
        if self.order_type == OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("限價單必須提供 limit_price")
            if not _is_finite_number(self.limit_price) or self.limit_price <= 0:
                raise ValueError("限價必須是有限且大於 0 的數字")


@dataclass
class OrderResult:
    """券商回報的成交/委託結果。"""

    ok: bool
    order_id: str = ""
    filled_quantity: float = 0.0
    avg_price: float = 0.0
    message: str = ""
    raw: dict = field(default_factory=dict)   # 券商原始回應,保留以利除錯


@dataclass
class Position:
    """目前持有的部位。"""

    symbol: str
    quantity: float          # 正=多單,負=空單
    avg_price: float
    market_price: float = 0.0
    cost_basis_known: bool = True

    @property
    def unrealized_pnl(self) -> float:
        if not self.cost_basis_known:
            raise ValueError("此部位沒有可靠成本基準，不能計算未實現損益")
        return (self.market_price - self.avg_price) * self.quantity


@dataclass
class AccountInfo:
    """帳戶概況。"""

    cash: float
    equity: float
    currency: str = "USD"


class BrokerAdapter(ABC):
    """所有券商連接器的共同介面。

    交易者要接自己的券商,只需繼承這個類別並實作下列方法。
    策略程式只會呼叫這些方法,因此換券商不必改策略。

    安全護欄:
        is_live 屬性標示「這是不是真實下單」。框架在送出真實訂單前,
        會檢查 confirm_live_trading() 是否被明確開啟 —— 預設關閉,
        避免交易者在還沒準備好時就誤觸真錢下單。
    """

    #: 人類可讀名稱
    name: str = "abstract"

    #: 是否為真實下單(PaperBroker 為 False)
    is_live: bool = False

    def __init__(self) -> None:
        self._live_confirmed = False

    # ── 安全閘門 ──────────────────────────────────────────
    def confirm_live_trading(self, *, i_understand_the_risk: bool = False) -> None:
        """明確開啟真實下單。預設關閉。

        交易者必須親手呼叫並傳入 i_understand_the_risk=True,
        才能解除真實下單的封鎖。這是刻意的摩擦,逼你停下來想清楚。
        只有字面 True 可解鎖；字串 / 整數 / None 一律視為失敗並維持關閉。
        """
        if i_understand_the_risk is not True:
            self._live_confirmed = False
            raise PermissionError(
                "要開啟真實下單,必須明確傳入 i_understand_the_risk=True。\n"
                "請先確認:策略已通過統計與樣本外驗證、已充分紙上模擬、"
                "已設好部位上限與停損,且你願意自負一切後果。"
            )
        self._live_confirmed = True

    def _guard_live(self) -> None:
        """送出真實訂單前的最後一道檢查。"""
        if self.is_live and self._live_confirmed is not True:
            raise PermissionError(
                f"[{self.name}] 真實下單已被安全閘門攔下。\n"
                "請先呼叫 broker.confirm_live_trading(i_understand_the_risk=True)。"
            )

    # ── 交易者需實作的抽象方法 ───────────────────────────
    @abstractmethod
    def connect(self) -> None:
        """建立連線 / 驗證金鑰。"""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """查詢帳戶概況。"""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """查詢目前持倉。"""

    @abstractmethod
    def get_price(self, symbol: str) -> float:
        """查詢最新成交價。"""

    @abstractmethod
    def place_order(self, order: Order) -> OrderResult:
        """送出委託單。實作時務必在最前面呼叫 self._guard_live()。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """取消委託。"""


# ======================================================================
# PaperBroker(紙上模擬)
# ======================================================================

class PaperBroker(BrokerAdapter):
    """以記憶體模擬的紙上券商。

    預設只支援『做多』(買進後賣出平倉):賣出超過持倉會直接拒單。
    若要模擬放空,明確傳入 allow_short=True —— 此時採 naive 現金會計:
    開空/回補的損益方向正確,但**不含**借券費、融資利息、資金費率、
    保證金鎖定與強制平倉。這是刻意的取捨:與其給出「半真」的保證金模型
    讓人誤以為已建模成本,不如誠實標明這是簡化模型(見 SHORT_DISCLAIMER)。

    Args:
        cash:        起始資金
        fee_rate:    每筆成交的手續費率(模擬交易成本)
        slippage:    市價單的模擬滑價率
        price_feed:  可選的報價函式 symbol -> price;未提供時需用 set_price 餵價
        allow_short: 是否允許裸放空(預設 False)。開啟後為簡化模型,估值偏樂觀。
    """

    name = "paper"
    is_live = False

    #: 開啟 allow_short 時,對使用者顯示的免責說明(刻意冗長,逼你讀完)
    SHORT_DISCLAIMER = (
        "⚠️ 放空為『簡化模型』:未計入借券費 / 融券手續費 / 融資利息 / "
        "資金費率 / 保證金鎖定 / 維持率與強制平倉。估值偏樂觀,"
        "僅供方向性損益驗證,不可當作真實放空的績效預期。"
    )

    def __init__(
        self,
        cash: float = 1_000_000.0,
        *,
        fee_rate: float = 0.001,
        slippage: float = 0.0005,
        currency: str = "USD",
        price_feed=None,
        allow_short: bool = False,
    ) -> None:
        super().__init__()
        if not _is_finite_number(cash) or cash < 0:
            raise ValueError("cash 必須是有限且 >= 0 的數字")
        if not _is_finite_number(fee_rate) or fee_rate < 0:
            raise ValueError("fee_rate 必須是有限且 >= 0 的數字")
        if not _is_finite_number(slippage) or not (0 <= slippage < 1):
            raise ValueError("slippage 必須滿足 0 <= slippage < 1")
        if type(allow_short) is not bool:
            raise ValueError("allow_short 必須是字面 bool")
        self._cash = float(cash)
        self._fee_rate = float(fee_rate)
        self._slippage = float(slippage)
        self._currency = currency
        self._price_feed = price_feed
        # 預設 False:賣出超過持倉直接拒單。設為 True 時允許裸放空,
        # 但採 naive 現金會計(見 SHORT_DISCLAIMER),不假裝已建模借券成本。
        self._allow_short = allow_short
        self._prices: dict[str, float] = {}
        self._positions: dict[str, Position] = {}
        self._order_seq = 0
        self.fills: list[OrderResult] = []   # 成交紀錄,方便事後分析

    # ── 報價 ──────────────────────────────────────────────
    def set_price(self, symbol: str, price: float) -> None:
        """手動餵入最新報價(回測 / 模擬時逐根餵價)。"""
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("報價代號不可空白")
        if not _is_finite_number(price) or price <= 0:
            raise ValueError("報價必須是有限且大於 0 的數字")
        self._prices[symbol] = float(price)
        if symbol in self._positions:
            self._positions[symbol].market_price = float(price)

    def get_price(self, symbol: str) -> float:
        if self._price_feed is not None:
            price = self._price_feed(symbol)
            self.set_price(symbol, price)
            return self._prices[symbol]
        if symbol not in self._prices:
            raise ValueError(
                f"PaperBroker 沒有 {symbol} 的報價。請先 set_price() 或提供 price_feed。"
            )
        return self._prices[symbol]

    # ── 帳戶 / 持倉 ───────────────────────────────────────
    def connect(self) -> None:
        # 紙上模擬無需連線
        pass

    def get_account(self) -> AccountInfo:
        if not _is_finite_number(self._cash):
            raise ValueError("紙上帳戶現金不是有限數字")

        # 不可用 sum() 靜默累加：即使每個價格、數量都有限，極端部位的
        # 乘積或累加仍可能溢位成 inf。這種帳戶快照沒有意義，必須拒絕而非
        # 回傳看似正常的 AccountInfo。
        equity = self._cash
        for position in self._positions.values():
            if not all(
                _is_finite_number(value)
                for value in (
                    position.quantity,
                    position.avg_price,
                    position.market_price,
                )
            ):
                raise ValueError("紙上帳戶部位含非有限數字")
            mark = self._prices.get(position.symbol, position.avg_price)
            if not _is_finite_number(mark) or mark <= 0:
                raise ValueError("紙上帳戶報價不是有限正數")
            position_value = position.quantity * mark
            if not _is_finite_number(position_value):
                raise ValueError("紙上帳戶部位市值不是有限數字")
            next_equity = equity + position_value
            if not _is_finite_number(next_equity):
                raise ValueError("紙上帳戶權益計算結果不是有限數字")
            equity = next_equity
        return AccountInfo(cash=self._cash, equity=equity, currency=self._currency)

    def get_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.quantity != 0]

    # ── 下單(模擬撮合)────────────────────────────────────
    def place_order(self, order: Order) -> OrderResult:
        self._guard_live()   # PaperBroker is_live=False,永遠放行;保留以示範正確用法
        order.validate()

        ref_price = self.get_price(order.symbol)
        if not _is_finite_number(ref_price) or ref_price <= 0:
            raise ValueError("報價必須是有限且大於 0 的數字")

        # 市價單套用滑價;限價單僅在「當下可立即成交」時以現價撮合(無掛單佇列)
        if order.order_type == OrderType.MARKET:
            slip = self._slippage if order.side == OrderSide.BUY else -self._slippage
            fill_price = ref_price * (1 + slip)
        else:
            limit = float(order.limit_price)  # validate() 已保證有限正數
            if order.side == OrderSide.BUY and limit < ref_price:
                return OrderResult(
                    ok=False,
                    message=(
                        f"買進限價 {limit:g} 低於現價 {ref_price:g},目前無法立即成交。"
                        " immediate-fill simulation has no pending orders;"
                        " 非可立即成交的限價單已拒單,帳戶狀態未變。"
                    ),
                )
            if order.side == OrderSide.SELL and limit > ref_price:
                return OrderResult(
                    ok=False,
                    message=(
                        f"賣出限價 {limit:g} 高於現價 {ref_price:g},目前無法立即成交。"
                        " immediate-fill simulation has no pending orders;"
                        " 非可立即成交的限價單已拒單,帳戶狀態未變。"
                    ),
                )
            # 可立即成交:以現價成交,且不超過買進上限 / 不低於賣出下限
            if order.side == OrderSide.BUY:
                fill_price = min(ref_price, limit)
            else:
                fill_price = max(ref_price, limit)

        notional = fill_price * order.quantity
        fee = abs(notional) * self._fee_rate
        if (
            not _is_finite_number(fill_price)
            or fill_price <= 0
            or not _is_finite_number(notional)
            or notional <= 0
            or not _is_finite_number(fee)
        ):
            return OrderResult(ok=False, message="成交金額計算結果無效,拒單且未改動帳戶")

        signed_qty = order.quantity if order.side == OrderSide.BUY else -order.quantity

        if order.side == OrderSide.BUY:
            # 買進:扣現金(含手續費)
            cost = notional + fee
            if not _is_finite_number(cost):
                return OrderResult(ok=False, message="成交金額計算結果非有限數字,拒單且未改動帳戶")
            if cost > self._cash:
                return OrderResult(
                    ok=False, message=f"資金不足:需 {cost:,.2f},現金 {self._cash:,.2f}"
                )
        else:
            # 賣出。預設(allow_short=False)只支援做多:賣出不得超過持倉。
            # 裸放空牽涉保證金、借券費、強制平倉,正確建模太複雜;
            # 與其給出錯誤的現金/權益數字,預設誠實拒單。
            held = self._positions.get(order.symbol)
            held_qty = held.quantity if held else 0.0
            # 不能以 epsilon 放行任何裸空；例如空持倉賣出 5e-10，雖小仍是
            # 真正的超賣，且會留下難以察覺的微小空單。
            if not self._allow_short and order.quantity > held_qty:
                return OrderResult(
                    ok=False,
                    message=(
                        f"賣出數量 {order.quantity:g} 超過持有部位 {held_qty:g}。"
                        "本紙上模擬預設只支援做多;要模擬放空請用 "
                        "PaperBroker(allow_short=True)(簡化模型,不含借券費)。"
                    ),
                )
            # 平多倉 / (allow_short 時)開空:回收賣出金額,扣手續費
            cost = -notional + fee
            if not _is_finite_number(cost):
                return OrderResult(ok=False, message="成交金額計算結果非有限數字,拒單且未改動帳戶")

        new_cash = self._cash - cost
        if not _is_finite_number(new_cash):
            return OrderResult(ok=False, message="現金結算結果非有限數字,拒單且未改動帳戶")

        # 在任何帳戶狀態變更前先完整預演部位。除了數量，舊成本、總成本與
        # 加權平均價也都可能在極端有限輸入下溢位成 inf，必須一併擋下。
        try:
            preview_position = self._preview_fill(
                order.symbol, signed_qty, fill_price
            )
        except ValueError as exc:
            return OrderResult(ok=False, message=f"{exc},拒單且未改動帳戶")

        self._cash = new_cash
        self._positions[order.symbol] = preview_position

        # 若這筆成交讓部位變成淨空單,附上放空的簡化模型免責說明
        pos_after = self._positions.get(order.symbol)
        msg = "paper fill"
        if pos_after is not None and pos_after.quantity < 0:
            msg = f"paper fill / {self.SHORT_DISCLAIMER}"

        self._order_seq += 1
        result = OrderResult(
            ok=True,
            order_id=f"PAPER-{self._order_seq:06d}",
            filled_quantity=order.quantity,
            avg_price=fill_price,
            message=msg,
            raw={"fee": fee, "tag": order.client_tag},
        )
        self.fills.append(result)
        return result

    def _preview_fill(
        self, symbol: str, signed_qty: float, price: float
    ) -> Position:
        """不改狀態地計算成交後持倉，所有算術通過後才由呼叫端提交。

        正確處理四種情形:
          1. 無部位 / 部位已平 → 直接建立新部位
          2. 同向加碼 → 加權平均成本
          3. 反向減碼(未超量)→ 數量減少,均價不變
          4. 反向超量(平舊倉並反手)→ 剩餘數量以「本次成交價」建立反向新部位
             (這是先前的 bug:超量翻倉時若沿用舊均價,未實現損益與
              停損停利會全部以錯誤的均價計算)
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.quantity == 0:
            if not _is_finite_number(signed_qty):
                raise ValueError("持倉數量計算結果非有限數字")
            return Position(symbol, signed_qty, price, price)

        if not all(
            _is_finite_number(value)
            for value in (pos.quantity, pos.avg_price, pos.market_price)
        ):
            raise ValueError("既有持倉含非有限數字")

        new_qty = pos.quantity + signed_qty
        if not _is_finite_number(new_qty):
            raise ValueError("持倉數量計算結果非有限數字")

        # 完全平倉
        if new_qty == 0:
            return Position(symbol, 0.0, pos.avg_price, price)

        same_direction = (pos.quantity > 0) == (signed_qty > 0)
        if same_direction:
            # 情形 2:同向加碼 → 加權平均
            total_cost = pos.avg_price * pos.quantity + price * signed_qty
            if not _is_finite_number(total_cost):
                raise ValueError("持倉加權成本計算結果非有限數字")
            new_avg = total_cost / new_qty
            if not _is_finite_number(new_avg) or new_avg <= 0:
                raise ValueError("持倉平均成本計算結果無效")
        elif (new_qty > 0) == (pos.quantity > 0):
            # 情形 3:反向減碼但未超量(方向不變)→ 均價維持原值
            new_avg = pos.avg_price
        else:
            # 情形 4:反向超量(方向翻轉)→ 剩餘部位以本次成交價為新均價
            new_avg = price

        return Position(symbol, new_qty, new_avg, price)

    def cancel_order(self, order_id: str) -> bool:
        # 紙上模擬為立即成交,無掛單可取消
        return False
