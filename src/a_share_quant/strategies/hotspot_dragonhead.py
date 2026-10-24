"""热点 + 龙头（研究选股规则）。

只回答「按规则持有哪些股票、各多少权重」，不回答该不该买卖，也不预测收益。

规则（先验，没有按收益搜索过）：

1. **热点**：股票池内，用截至 as_of 的后复权价算每只股票近 ``window`` 个交易日收益，
   再按行业取等权平均。有效样本数达到 ``min_industry_members`` 的行业里，
   收益最高的 ``top_k`` 个行业是热点。行业收益并列时按行业名升序，结果可复现。
2. **龙头**：每个热点行业内部，相对强弱 ``个股收益 - 行业等权收益`` 与
   近 ``amount_window`` 日日均成交额各自排名（高者秩大），两者相加。
   取得分最高的 ``top_m`` 只。并列时依次比相对强弱、成交额、代码升序。
3. **组合**：入选标的等权，权重和为 1。持仓数超过 ``max_positions`` 时，
   按「更热的行业优先、行业内名次优先」截断，不用代码字典序。
   单票 10% 上限不在这里做，交给 ``PortfolioRules``：不足 10 只时超出部分留成现金。

防未来函数：

* 只用 ``trade_date <= as_of`` 的行情（引擎已保证，这里再截一次）；
* 行业优先用 bars 里截至 as_of 的最后一条非空 ``industry``。
  没有逐日快照时才退回静态 ``industry_map``，并告警：那是当前分类，分组含前视。

缺数据：bars 与 ``industry_map`` 都给不出行业时返回空组合并告警，
不用全市场动量顶上。空仓表示缺数据，不是看空。

TODO: 研究结论依赖 ``bars.industry`` 的逐日历史快照。
``scripts/fetch_industry.py`` 写入的是 baostock 当前证监会行业，
只能用来把这条链路跑通，不能用来判断规则有没有效。
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

import pandas as pd

from ..data.schema import coerce_bool
from ..factors import pivot_column
from .base import Strategy, TargetPortfolio

MISSING_INDUSTRY_WARNING = (
    "hotspot_dragonhead: 缺少行业分类，返回空组合。"
    "需要 bars.industry（截至 as_of 的逐日快照，优先）"
    "或 industry_map（symbol -> 行业）。"
    "TODO: 接入逐日行业历史后再把回测数字当作研究结论。"
)

STATIC_INDUSTRY_WARNING = (
    "hotspot_dragonhead: 行业来自静态 industry_map（当前分类），"
    "不是截至 as_of 的逐日快照。热点分组含前视，"
    "这次回测只验证机制，不能当研究结论。"
    "需要的字段是 bars.industry。"
)

NO_AMOUNT_WARNING = (
    "hotspot_dragonhead: bars 没有可用的 amount，"
    "龙头只按相对强弱排序，成交额这一路没有生效。"
)

NO_HOTSPOT_WARNING = (
    "hotspot_dragonhead: 没有任何行业达到 min_industry_members，返回空组合。"
)

_EMPTY_INDUSTRY = {"", "nan", "none", "<na>", "nat"}


def _clean_industry_label(value) -> Optional[str]:
    """把行业标签收成非空字符串；缺失与空串返回 None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if text.lower() in _EMPTY_INDUSTRY:
        return None
    return text


def static_industry_map(instruments: pd.DataFrame) -> Optional[dict[str, str]]:
    """从 instruments 抽出 symbol -> industry。没有可用行业时返回 None。"""
    if instruments is None or instruments.empty or "industry" not in instruments.columns:
        return None
    out: dict[str, str] = {}
    for sym, ind in zip(instruments["symbol"], instruments["industry"]):
        label = _clean_industry_label(ind)
        if label is None:
            continue
        out[str(sym)] = label
    return out or None


class HotspotDragonhead(Strategy):
    """热点行业里的龙头，等权。

    参数
    ----
    top_k                 热点行业个数
    top_m                 每个热点行业入选的龙头只数
    window                行业收益与相对强弱的回看交易日
    amount_window         日均成交额回看交易日
    min_industry_members  行业内有效收益样本少于此数则不参与热点排名
    industry_map          静态代码 -> 行业；仅在 bars 没有逐日 industry 时使用
    max_positions         入选数上限；None 表示不在策略内截断
    """

    name = "hotspot_dragonhead"

    def __init__(self, top_k: int = 5, top_m: int = 2, window: int = 20,
                 amount_window: int = 20, min_industry_members: int = 3,
                 industry_map: Optional[Mapping[str, str]] = None,
                 max_positions: Optional[int] = None) -> None:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if top_m < 1:
            raise ValueError("top_m must be >= 1")
        if window < 1:
            raise ValueError("window must be >= 1")
        if amount_window < 1:
            raise ValueError("amount_window must be >= 1")
        if min_industry_members < 2:
            raise ValueError("min_industry_members must be >= 2")
        if max_positions is not None and max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        self.top_k = int(top_k)
        self.top_m = int(top_m)
        self.window = int(window)
        self.amount_window = int(amount_window)
        self.min_industry_members = int(min_industry_members)
        cleaned: dict[str, str] = {}
        for key, value in dict(industry_map or {}).items():
            label = _clean_industry_label(value)
            if label is not None:
                cleaned[str(key)] = label
        self.industry_map = cleaned or None
        self.max_positions = None if max_positions is None else int(max_positions)
        self.warmup = max(self.window + 1, self.amount_window)
        self.industry_source = "unresolved"
        # 与 UniverseBuilder 相同：告警进 self.warnings，引擎在信号日抄走。
        # 每个信号日先清空，_warned 保证同一句全回测只出现一次。
        self.warnings: list[str] = []
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(message)

    def _empty(self, ts: pd.Timestamp) -> TargetPortfolio:
        return TargetPortfolio(as_of=ts, weights={})

    def _labels_from_bars(self, sub: pd.DataFrame, ts: pd.Timestamp,
                          pool: set[str]) -> pd.Series:
        """每只股票截至 as_of 的最后一条非空行业。不用 as_of 之后的行。"""
        if "industry" not in sub.columns or sub.empty:
            return pd.Series(dtype=object)
        hist = sub.loc[sub["symbol"].isin(pool) & (sub["trade_date"] <= ts),
                       ["trade_date", "symbol", "industry"]]
        if hist.empty:
            return pd.Series(dtype=object)
        labels = hist["industry"].map(_clean_industry_label)
        hist = hist.loc[labels.notna()].copy()
        if hist.empty:
            return pd.Series(dtype=object)
        hist["industry"] = labels.loc[labels.notna()].astype(str)
        last = (hist.sort_values(["symbol", "trade_date"], kind="mergesort")
                    .groupby("symbol", sort=False)
                    .tail(1))
        return last.set_index(last["symbol"].astype(str))["industry"]

    def _labels_from_map(self, pool: set[str]) -> pd.Series:
        if not self.industry_map:
            return pd.Series(dtype=object)
        data: dict[str, str] = {}
        for sym in pool:
            label = _clean_industry_label(self.industry_map.get(str(sym)))
            if label is not None:
                data[str(sym)] = label
        return pd.Series(data, dtype=object)

    def _blocked_symbols(self, sub: pd.DataFrame, ts: pd.Timestamp) -> set[str]:
        """截至 as_of 最后一条状态为 ST 或停牌的代码。

        列不存在就不筛——股票池已经按 status 表剔除过；这里只在行情上
        带了标记时再挡一层（status 缺行时宇宙会放行）。
        """
        cols = [c for c in ("is_st", "is_suspended") if c in sub.columns]
        if not cols or sub.empty:
            return set()
        hist = sub.loc[sub["trade_date"] <= ts, ["trade_date", "symbol", *cols]]
        if hist.empty:
            return set()
        last = (hist.sort_values(["symbol", "trade_date"], kind="mergesort")
                    .groupby("symbol", sort=False)
                    .tail(1))
        blocked: set[str] = set()
        for col in cols:
            flag = coerce_bool(last[col])
            blocked.update(last.loc[flag, "symbol"].astype(str).tolist())
        return blocked

    def generate_target(self, as_of, bars: pd.DataFrame,
                        universe: Sequence[str]) -> TargetPortfolio:
        ts = pd.Timestamp(as_of).normalize()
        self.warnings = []
        pool = {str(s) for s in universe}
        if not pool or bars is None or bars.empty:
            self.industry_source = "missing"
            return self._empty(ts)

        sub = bars[bars["symbol"].astype(str).isin(pool)]
        sub = sub[sub["trade_date"] <= ts]
        if sub.empty:
            self.industry_source = "missing"
            return self._empty(ts)
        sub = sub.copy()
        sub["symbol"] = sub["symbol"].astype(str)

        pool -= self._blocked_symbols(sub, ts)
        if not pool:
            self.industry_source = "missing"
            return self._empty(ts)
        sub = sub[sub["symbol"].isin(pool)]

        labels = self._labels_from_bars(sub, ts, pool)
        if not labels.empty:
            # 有逐日快照就只用它，不用静态表把没标注的股票补进来。
            self.industry_source = "bars"
        else:
            labels = self._labels_from_map(pool)
            if labels.empty:
                self.industry_source = "missing"
                self._warn_once("missing_industry", MISSING_INDUSTRY_WARNING)
                return self._empty(ts)
            self.industry_source = "static"
            self._warn_once("static_industry", STATIC_INDUSTRY_WARNING)

        col = "adj_close" if "adj_close" in sub.columns else "close"
        px = pivot_column(sub, col).sort_index()
        if len(px) < self.window + 1:
            return self._empty(ts)
        end = px.iloc[-1]
        start = px.iloc[-1 - self.window]
        ret = (end / start - 1.0).replace([float("inf"), float("-inf")], pd.NA)
        ret = pd.to_numeric(ret, errors="coerce").dropna()
        ret.index = ret.index.map(str)
        ret = ret[ret.index.isin(labels.index)]
        if ret.empty:
            return self._empty(ts)

        labeled = ret.to_frame("ret")
        labeled["industry"] = labels.reindex(labeled.index)
        labeled = labeled.dropna(subset=["industry"])
        stats = (labeled.groupby("industry", sort=False)["ret"]
                        .agg(mean="mean", count="count")
                        .reset_index())
        stats = stats[stats["count"] >= self.min_industry_members]
        if stats.empty:
            self._warn_once("no_hotspot", NO_HOTSPOT_WARNING)
            return self._empty(ts)
        stats = stats.sort_values(["mean", "industry"],
                                   ascending=[False, True], kind="mergesort")
        hot_names = stats["industry"].head(self.top_k).tolist()
        sector_mean = dict(zip(stats["industry"], stats["mean"]))

        use_amount = "amount" in sub.columns
        avg_amount = pd.Series(dtype=float)
        if use_amount:
            amt = pivot_column(sub, "amount").sort_index()
            avg_amount = pd.to_numeric(
                amt.tail(self.amount_window).mean(), errors="coerce").dropna()
            avg_amount.index = avg_amount.index.map(str)
            avg_amount = avg_amount[avg_amount > 0]
            if avg_amount.empty:
                use_amount = False
        if not use_amount:
            self._warn_once("no_amount", NO_AMOUNT_WARNING)

        frames: list[pd.DataFrame] = []
        for name in hot_names:
            members = labeled.index[labeled["industry"] == name]
            one = labeled.loc[members, "ret"]
            if use_amount:
                amt_m = avg_amount.reindex(one.index)
                keep = amt_m.notna()
                one = one.loc[keep]
                amt_m = amt_m.loc[keep]
                if one.empty:
                    continue
            else:
                amt_m = pd.Series(0.0, index=one.index)
            rs = one - float(sector_mean[name])
            score = rs.rank(method="average", ascending=True)
            if use_amount:
                score = score + amt_m.rank(method="average", ascending=True)
            frame = pd.DataFrame({
                "symbol": one.index.astype(str),
                "rs": rs.to_numpy(),
                "amount": amt_m.reindex(one.index).to_numpy(),
                "score": score.reindex(one.index).to_numpy(),
            })
            frame = frame.sort_values(
                ["score", "rs", "amount", "symbol"],
                ascending=[False, False, False, True],
                kind="mergesort")
            frames.append(frame.head(self.top_m))

        if not frames:
            return self._empty(ts)
        picked = pd.concat(frames, ignore_index=True)
        # 拼接顺序就是「更热的行业在前、行业内名次在前」。
        # 不按分数全局重排：组内秩与行业人数有关，跨行业不能比。
        if self.max_positions is not None:
            picked = picked.head(self.max_positions)
        symbols = picked["symbol"].astype(str).tolist()
        if not symbols:
            return self._empty(ts)
        w = 1.0 / len(symbols)
        return TargetPortfolio(as_of=ts, weights={s: w for s in symbols})
