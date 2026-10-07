# binance_bot

由 **反詐投資王(Anti-Gambling Trader)** 腳架產生的個人交易程式。

- 市場:`crypto`
- 標的:BTCUSDT, ETHUSDT
- 券商:Binance(加密貨幣)
- 圖表:Lightweight Charts (TradingView)（Apache-2.0）

## ⛔ 來自反詐投資王的重要提醒

你的交易紀錄分析結果為:**(尚未完成含樣本外與風險基準的交易階段分析)**

因此本專案的 `main.py` 已**預設禁用真實下單**(`ALLOW_LIVE_TRADING = False`)。
請先修正方法,用後續未看過的新交易重新驗證,再以 `--from-analysis` 重新產生專案。
不要手動猜測或改寫 `stage_code` / `allow_live_trading` —— 這是保護你的錢,不是限制你。

## 快速開始（紙上模擬，不碰真錢）

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python main.py            # 用 PaperBroker 跑一遍,並產生圖表與交易紀錄
```

跑完會輸出績效摘要、`chart.html` 圖表,以及 `paper_trades.csv`(已平倉交易)。

### 三步驟:規則 → 真實資料回測 → 統計檢驗

1. **規則**:`strategy.py` 目前放的是「均線交叉」**範例**(MA5 上穿 MA20 買、
   下穿或停損 5% / 停利 15% 賣,只做多)。這不是驗證過的賺錢方法,請換成你自己的規則。
2. **真實資料**:在 `config.yaml` 把 `data.source` 改成 `binance`,程式會從 Binance
   公開行情 API 抓最近 `data.bars` 根**已收盤** K 線(不需要 API key,不會下單),
   並把原始資料存到 `data_cache/` 方便你檢查。預設 `demo` 是假資料,績效沒有意義。
3. **檢驗**:回到 repo 根目錄,把交易紀錄交給反詐投資王:

   ```bash
   python -m core.cli analyze my_bots/binance_bot/paper_trades.csv
   ```

   回測紀錄是「事後套規則」的結果,就算通過也只代表值得繼續紙上觀察,
   不代表能上真錢 —— 參數一調再調直到回測好看,正是過度擬合的典型陷阱。

## 專案結構

```
binance_bot/
  main.py            # 主程式（預設紙上模擬）
  strategy.py        # 你的交易規則（進出場條件待你填寫）
  broker_lib.py      # 自包含的券商函式庫（交易介面 + PaperBroker，零外部相依）
  broker_setup.py    # 選擇 / 建立券商連接器
  brokers/           # 真實券商範例框架（待填 API key 與實作）
  charting.py        # 圖表模組（Lightweight Charts (TradingView)）
  data_feed.py       # 資料來源（回測 / 即時）
  config.example.yaml # 設定範本（複製成 config.yaml 後填入）
```

> 本專案**自包含**：不需安裝反詐投資王本體即可獨立執行。

## 接你自己的券商

券商:Binance(加密貨幣)
安裝:`pip install python-binance`

> ⚠️ 在 Binance 後台建立 API key 時,先只開『讀取』權限做測試;確認程式無誤後再考慮開啟交易權限。永遠不要開提領權限。

1. 打開 `brokers/` 下的範例框架,依說明用環境變數或設定提供連線資料,並完成 `TODO`。
2. 在 `broker_setup.py` 把 `build_broker()` 改成回傳你的券商實例。
3. **務必先用券商的測試網 / 模擬模式確認無誤。**

## 從紙上模擬切到真實下單（高風險）

真實下單受**安全閘門**保護。要解除,必須:

1. 用 `scaffold --from-analysis <完整交易紀錄>` 產生專案;只有階段為
   `tiny_live_validation` 時,設定範本才可能同時寫入正確 stage 與允許旗標。
2. 複製 `config.example.yaml` 為 `config.yaml`,閱讀免責聲明後才把
   `risk.i_have_read_disclaimer` 設為布林值 `true`。不要手動改 stage 或允許旗標。
3. 在 `main.py` 把 `ALLOW_LIVE_TRADING` 改為 `True`。
4. Runtime 會重新驗證上述所有設定,最後才呼叫券商的
   `confirm_live_trading(i_understand_the_risk=True)`。
5. `main.py` 內建的是歷史／示範 K 線 replay，因此偵測到 live broker 時仍會硬性退出；
   不會把 120 根歷史訊號一次送成真單。真實驗證必須另寫只處理「最新一根已完成 K 線」
   的 runner，並接上可證明為即時且無前視的資料來源。

缺欄位、錯誤型別、其他 stage 或 YAML 損壞都會維持封鎖。即使通過全部閘門,
也只代表程式允許你自行做極小額驗證,不代表適合重押或全職交易。

這些摩擦是刻意設計的 —— 讓你在動用真錢前,被迫停下來想清楚。

## 換圖表樣式

想換成別的圖表庫,重新用反詐投資王產生:

```bash
python -m core.cli scaffold --name binance_bot --broker binance \
    --chart <lightweight|plotly|mplfinance|echarts> --market crypto
```

## ⚠️ 免責聲明

本專案為教育與研究用途,不構成投資建議。投資有風險,盈虧自負。
過去績效不代表未來表現。你對自己用本程式做出的一切交易負全部責任。

---

由 **反詐投資王(Anti-Gambling Trader)** 腳架產生。
原作者:好棒棒反詐協會 - 免費顧問 阿軒割割
