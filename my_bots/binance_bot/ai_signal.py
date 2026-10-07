"""AI 市場觀點訊號(Claude)—— 參考 virattt/ai-hedge-fund 的設計原則:
「AI 只給觀點,不碰下單」。Claude 只回傳一個 -1 ~ +1 的看法分數與一句理由,
要不要買、買多少、停損停利,全部由 strategy.py 的固定規則決定。

⚠️ 回測最大的陷阱:模型的訓練資料裡已經有那段歷史行情。問它 2024 年的 BTC,
它其實「知道」後來漲跌 —— 這種回測績效是假的。因此:
  1. 只在模型知識截止日「之後」的 K 線讓 AI 做決策(ai.decide_after),
     之前的 K 線只當作歷史背景。
  2. 預設匿名化:不告訴模型標的名稱與日期,價格換算成以 100 為起點的指數。
     這能降低、但無法完全消除「認出行情」的可能。

成本控制:每根 K 線 × 每個標的都是一次 API 呼叫。執行前會先估算呼叫次數與粗估費用,
超過 ai.max_calls 會直接停止,不會先花錢再告訴你。同樣的輸入會快取在 ai_cache/,
重跑回測不會重複付費,也讓結果可以重現(新模型不支援 temperature,快取是唯一的重現方式)。
"""

import hashlib
import json
import os
from datetime import datetime, timezone

# 模型「沒看過」的第一天 = 官方知識截止月份的下一天。只有這天之後的行情才能拿來回測。
# 換成其他模型時,請依官方公布的截止日補上,不知道就不要猜 —— 改在 config 明確設定 decide_after。
MODEL_FIRST_UNSEEN_DAY = {
    "claude-opus-5-5": "2026-07-01",   # 官方:Knowledge cutoff June 2026
}

# 粗估每次呼叫的 token 數與單價(USD / 百萬 token),只用來事前警示,實際以帳單為準
_EST_INPUT_TOKENS = 2_000
_EST_OUTPUT_TOKENS = 1_000
_PRICE_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00)}

_CACHE_DIR = "ai_cache"

_SYSTEM_PROMPT = """You are a disciplined, skeptical market analyst inside a paper-trading research tool.
You will see only an anonymized recent price history of one asset: prices are rebased so the first bar equals 100, and there are no names or dates.
Judge whether the evidence in this data favors the price being higher or lower over the next few bars.

Rules:
- Base your view only on the data shown. Do not try to identify the asset or the time period.
- Short-term price moves are mostly noise. When there is no clear evidence, return a conviction close to 0. Large magnitudes (above 0.6) should be rare.
- conviction: a number from -1 (strongly expect lower) to +1 (strongly expect higher).
- thesis: one short sentence in Traditional Chinese (at most 60 characters) naming the main evidence."""

_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "conviction": {"type": "number"},
            "thesis": {"type": "string"},
        },
        "required": ["conviction", "thesis"],
        "additionalProperties": False,
    },
}


def _to_ts(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _rsi(closes: list, n: int = 14) -> float:
    if len(closes) <= n:
        return 50.0
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


class AISignal:
    def __init__(self, config: dict, client=None):
        ai = config.get("ai") or {}
        self.model = str(ai.get("model", "claude-opus-5-5"))
        self.effort = str(ai.get("effort", "low"))
        self.lookback = int(ai.get("lookback_bars", 60))
        self.anonymize = bool(ai.get("anonymize", True))
        self.max_calls = int(ai.get("max_calls", 200))
        self.interval = str((config.get("data") or {}).get("interval", "1d"))

        decide_after = ai.get("decide_after")
        cutoff = MODEL_FIRST_UNSEEN_DAY.get(self.model)
        if not decide_after:
            if cutoff is None:
                raise SystemExit(
                    f"⛔ 不知道 {self.model} 的知識截止日,無法防止回測偷看未來。\n"
                    "   請在 config.yaml 的 ai.decide_after 填入該模型知識截止日之後的第一天(YYYY-MM-DD)。"
                )
            decide_after = cutoff
        self.decide_after = str(decide_after)
        self.decide_after_ts = _to_ts(self.decide_after)
        if cutoff and self.decide_after < cutoff:
            print(f"⚠️ ai.decide_after={self.decide_after} 早於 {self.model} 沒看過的第一天 {cutoff}:\n"
                  "   這段期間的回測,模型可能已經知道答案,績效會被高估。")

        self._client = client
        self.calls = 0          # 實際付費呼叫次數
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.decisions = []     # 每次判斷的紀錄,供事後檢查

    # ── 事前估算 ─────────────────────────────────────────
    def can_decide(self, history: list) -> bool:
        return (bool(history) and history[-1]["time"] >= self.decide_after_ts
                and len(history) >= self.lookback)

    def preflight(self, histories: dict) -> None:
        """跑之前先算出需要幾次呼叫,超過預算就停,不先花錢。"""
        needed = 0
        for hist in histories.values():
            for i in range(len(hist)):
                window = hist[: i + 1]
                if self.can_decide(window) and not os.path.exists(self._cache_path(self._prompt(window))):
                    needed += 1
        in_price, out_price = _PRICE_PER_MTOK.get(self.model, (None, None))
        est = ""
        if in_price is not None:
            usd = needed * (_EST_INPUT_TOKENS * in_price + _EST_OUTPUT_TOKENS * out_price) / 1e6
            est = f",粗估約 US${usd:,.2f}(實際以帳單為準)"
        print(f"🤖 AI 訊號:模型 {self.model}(effort={self.effort}),只在 {self.decide_after} 之後的 K 線做決策")
        print(f"   需要新的 API 呼叫 {needed} 次{est};上限 ai.max_calls={self.max_calls}")
        if needed > self.max_calls:
            raise SystemExit(
                f"⛔ 需要 {needed} 次呼叫,超過 ai.max_calls={self.max_calls},為避免意外帳單已停止。\n"
                "   確認願意付費後,再調高 config.yaml 的 ai.max_calls,或減少標的 / 改用較長的 K 線週期。"
            )
        if needed == 0 and not any(self.can_decide(h) for h in histories.values()):
            print("   ⚠️ 沒有任何 K 線落在 decide_after 之後(示範資料都是舊日期),AI 不會做任何決策。")

    # ── 主要功能 ─────────────────────────────────────────
    def assess(self, symbol: str, history: list) -> tuple:
        """回傳 (conviction, thesis)。只能在 can_decide(history) 為 True 時呼叫。"""
        prompt = self._prompt(history)
        path = self._cache_path(prompt)
        result = self._read_cache(path)
        if result is not None:
            self.cache_hits += 1
        else:
            if self.calls >= self.max_calls:
                raise SystemExit(f"⛔ 已達 ai.max_calls={self.max_calls},停止呼叫 API。")
            result = self._call(prompt)
            if result.get("cacheable"):
                self._write_cache(path, result)
        conviction = max(-1.0, min(1.0, float(result["conviction"])))
        thesis = str(result["thesis"])
        self.decisions.append({"time": history[-1]["time"], "symbol": symbol,
                               "conviction": conviction, "thesis": thesis})
        return conviction, thesis

    def _prompt(self, history: list) -> str:
        window = history[-self.lookback:]
        base = window[0]["close"]
        closes = [c["close"] for c in window]
        avg_vol = sum(c["volume"] for c in window) / len(window) or 1.0
        rows = []
        for i, c in enumerate(window):
            label = f"t{i - len(window) + 1}" if self.anonymize else \
                datetime.fromtimestamp(c["time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            rows.append(f"{label},{c['open'] / base * 100:.2f},{c['high'] / base * 100:.2f},"
                        f"{c['low'] / base * 100:.2f},{c['close'] / base * 100:.2f},"
                        f"{c['volume'] / avg_vol:.2f}")
        ma_fast = sum(closes[-5:]) / min(5, len(closes))
        ma_slow = sum(closes[-20:]) / min(20, len(closes))
        rets = [b / a - 1 for a, b in zip(closes[-21:-1], closes[-20:])]
        vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 if rets else 0.0
        header = "Asset: anonymized" if self.anonymize else "Asset: (named mode)"
        return (
            f"{header}. Bar interval: {self.interval}. Last row (t0) is the latest closed bar.\n"
            f"Indicators at t0: MA5/MA20 = {ma_fast / ma_slow:.4f}, RSI14 = {_rsi(closes):.1f}, "
            f"20-bar return stdev = {vol * 100:.2f}%\n"
            "bar,open,high,low,close,relative_volume\n" + "\n".join(rows)
        )

    def _call(self, prompt: str) -> dict:
        client = self._client or self._make_client()
        import anthropic
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=_SYSTEM_PROMPT,
                output_config={"effort": self.effort, "format": _OUTPUT_SCHEMA},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError as exc:
            raise SystemExit("⛔ Anthropic API 金鑰無效(環境變數 ANTHROPIC_API_KEY)。") from exc
        except TypeError as exc:
            # SDK 找不到任何憑證時在送出前就丟 TypeError;其他 TypeError 照常往外拋
            if "authentication" not in str(exc):
                raise
            raise SystemExit(
                "⛔ 找不到 Anthropic API 金鑰。請自己在終端機設定環境變數 ANTHROPIC_API_KEY\n"
                "   (或執行 ant auth login),不要把金鑰寫進 config.yaml 或程式碼。"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise SystemExit("⛔ 觸發 API 速率限制(SDK 已自動重試仍失敗),請稍後再跑;已完成的判斷都在快取裡。") from exc
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            # 不把錯誤當成「中性 0 分」—— 那會悄悄扭曲回測結果
            raise SystemExit(f"⛔ 呼叫 Claude 失敗:{exc}\n   已完成的判斷都在快取裡,修正後重跑即可。") from exc

        self.calls += 1
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0

        if resp.stop_reason == "refusal":
            return {"conviction": 0.0, "thesis": "模型拒答,視為無觀點", "cacheable": False}
        if resp.stop_reason == "max_tokens":
            raise SystemExit("⛔ 模型輸出被截斷(max_tokens),無法取得完整判斷。")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise SystemExit("⛔ 模型沒有回傳文字結果。")
        data = json.loads(text)
        return {"conviction": data["conviction"], "thesis": data["thesis"], "cacheable": True}

    @staticmethod
    def _make_client():
        try:
            import anthropic
        except ImportError as exc:
            raise SystemExit("⛔ 需要先安裝 Anthropic SDK:pip install anthropic") from exc
        return anthropic.Anthropic(max_retries=4)

    # ── 快取 ─────────────────────────────────────────────
    def _cache_path(self, prompt: str) -> str:
        key = json.dumps([self.model, self.effort, _SYSTEM_PROMPT, prompt], ensure_ascii=False)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(_CACHE_DIR, f"{digest}.json")

    @staticmethod
    def _read_cache(path: str):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_cache(path: str, result: dict) -> None:
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except OSError:
            pass

    def summary(self) -> str:
        return (f"AI 呼叫 {self.calls} 次(快取命中 {self.cache_hits} 次),"
                f"token 輸入 {self.input_tokens:,} / 輸出 {self.output_tokens:,}")
