"""Binance 券商連接器(範例框架 — 請填入你的實作)。"""

from broker_lib import (
    AccountInfo, BrokerAdapter, Order, OrderResult, OrderSide, OrderType, Position,
)


class BinanceBroker(BrokerAdapter):
    name = "binance"
    is_live = True   # 真實下單 —— 受安全閘門保護

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        super().__init__()
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet   # 強烈建議先用測試網
        self.client = None

    def connect(self) -> None:
        from binance.client import Client
        self.client = Client(self.api_key, self.api_secret, testnet=self.testnet)
        # TODO: 視需要驗證連線,例如 self.client.ping()

    def get_account(self) -> AccountInfo:
        acct = self.client.get_account()
        # TODO: 把 acct 解析成 cash / equity
        usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), {"free": 0})
        cash = float(usdt["free"])
        return AccountInfo(cash=cash, equity=cash, currency="USDT")

    def get_positions(self) -> list[Position]:
        # 現貨沒有傳統「持倉」概念,可用餘額代表
        # TODO: 依你的需求把非零幣別餘額轉成 Position
        return []

    def get_price(self, symbol: str) -> float:
        t = self.client.get_symbol_ticker(symbol=symbol)
        return float(t["price"])

    def place_order(self, order: Order) -> OrderResult:
        self._guard_live()   # 真實下單前的安全檢查,務必保留
        order.validate()
        side = "BUY" if order.side == OrderSide.BUY else "SELL"
        try:
            if order.order_type == OrderType.MARKET:
                resp = self.client.create_order(
                    symbol=order.symbol, side=side, type="MARKET",
                    quantity=order.quantity,
                )
            else:
                resp = self.client.create_order(
                    symbol=order.symbol, side=side, type="LIMIT",
                    timeInForce="GTC", quantity=order.quantity,
                    price=str(order.limit_price),
                )
            return OrderResult(
                ok=True, order_id=str(resp.get("orderId", "")),
                filled_quantity=float(resp.get("executedQty", 0)),
                avg_price=order.limit_price or self.get_price(order.symbol),
                raw=resp,
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, message=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        # TODO: self.client.cancel_order(symbol=..., orderId=order_id)
        raise NotImplementedError
