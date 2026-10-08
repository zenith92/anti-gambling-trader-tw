"""券商選擇:預設紙上模擬,接真實券商時改這裡。"""

from broker_lib import PaperBroker
# from brokers.binance_broker import BinanceBroker


def build_broker(config: dict):
    """依設定回傳券商實例。預設為安全的紙上模擬。"""
    # 預設仍回傳紙上模擬;要接真實券商,取消下面註解並填入你的金鑰。
    if config.get("broker") == "binance":
        # creds = config.get("credentials", {})
        # ⚠️ 建構子參數依券商而異(如 IBKR 是 host/port/client_id,不是金鑰)——
        #    先打開 brokers/binance_broker.py 看 BinanceBroker.__init__ 的簽名,
        #    再把對應欄位加進 config.yaml 的 credentials 區塊。
        # broker = BinanceBroker(...)  # ← 依上面確認的簽名填參數
        # return broker
        pass
    paper = config.get("paper", {})
    return PaperBroker(
        cash=paper.get("starting_cash", 10_000),
        fee_rate=paper.get("fee_rate", 0.001),
        slippage=paper.get("slippage", 0.0005),
        currency="USDT",
    )
