# R02 Grok

reviewed_commit: e41e053435c33f80970e5e1e6dfa2c5b68c2b160

HEAD 与指定前缀 `e41e053` 一致（`e41e053435c33f80970e5e1e6dfa2c5b68c2b160`）。下面引用的行在该 commit 上仍存在。本轮只回应 F-09、F-10。

### F-09

坚持。收窄为：仅当持仓没有零股、且本次卖出不是清仓时，成交量上限会记下一笔交易所不接受的非整百卖单。主板与创业板适用；科创板不在本条范围内。

第 3.3.8 条的两句话本身没有写「卖出必须是 100 股的整数倍」。现行原文（上交所、深交所 2026 年修订，均自 2026-07-06 施行）是：

> 通过竞价交易买入证券的，申报数量应当为 100 股（份）或其整数倍。卖出证券时，余额不足 100 股（份）的部分，应当一次性申报卖出。

- 上交所《交易规则（2026年修订）》第 3.3.8 条，上证发〔2026〕41 号：<https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml>
- 深交所《交易规则（2026年修订）》第 3.3.8 条，深证上〔2026〕551 号：<https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf>

只看第二句，250 股可以读成合法申报。交易所对这一句的解释不是这个读法。

上交所《关于进一步明确竞价交易申报数量及前端控制有关事项的通知》（上证函〔2014〕301 号，2014-06-27；页面仍在上交所规则指南栏目）：<http://www.sse.com.cn/lawandrules/guide/stock/jyglywznylc/tz/c/c_20230209_5716007.shtml>

通知当时引用的是旧编号第 3.4.7 条，句子与现在的第 3.3.8 条相同。第一条（一）：

> 持有单只股票、基金、权证的数量为 100 股（份）或其整数倍的，卖出申报数量应当为 100 股（份）或其整数倍，不得拆分成任何形式的零股申报卖出。

附件示例一：持有 500 股，可以拆成 200 股和 300 股；不可拆成 201 股和 299 股，「不可因拆分而形成含零股的部分」。持有 1000 股时申报卖出 250 股，就是把整手余额拆成含零股的申报。

深交所《关于严格执行证券竞价交易卖出申报数量有关规定的通知》（深证会〔2014〕72 号）第一条（一）对当时已编号为第 3.3.8 条的同一句规定：

> 持有单只股票、基金的数量为 100 股（份）或其整数倍的，卖出申报数量应当为 100 股（份）或其整数倍，不得拆分卖出。

创业板走同一条。深交所规则第 1.2 条覆盖本所上市股票；第 3.3.8 条在竞价交易的通用节，不按板块另写。第 3.3.9 条只把创业板限价单笔上限改为 30 万股、市价 15 万股。盘后固定价格交易第 3.6.7 条同样是买入 100 股整数倍、余额不足 100 股一次性卖出。科创板不适用：上交所第 6.7 条是单笔不少于 200 股、余额不足 200 股一次性卖出，250 股在科创板可以是合法申报。`config/universe.yaml` 的白名单是主板与创业板，并写明排除 688/689，本条不覆盖科创板。

代码在「持仓整百、目标卖出不是清仓」时会记出 250 股。`generate_orders` 先按整手生成减仓卖单：

```89:91:src/a_share_quant/backtest/execution.py
                cut = (cur_shares - desired) // lot_size * lot_size
                if cut > 0:
                    sell_orders.append(Order(sym, "sell", int(cut)))
```

卖出上限用默认 `lot_size=1`，买入才传入 `self.lot_size`：

```130:138:src/a_share_quant/backtest/execution.py
    def _volume_cap(self, row, lot_size: int = 1) -> Optional[int]:
        """单笔可成交股数上限；None 表示不限制。"""
        if self.max_participation_rate <= 0:
            return None
        vol = row.get("volume")
        if vol is None or pd.isna(vol):
            return None
        cap = int(float(vol) * self.max_participation_rate)
        return max(cap // lot_size * lot_size, 0)
```

```186:189:src/a_share_quant/backtest/execution.py
            cap = self._volume_cap(row)
            capped = cap is not None and shares > cap
            if capped:
                shares = cap
```

```226:227:src/a_share_quant/backtest/execution.py
            eligible.append((od, self.cost_model.buy_price(open_px),
                             self._volume_cap(row, self.lot_size)))
```

引擎在 `engine.py:253` 把上述订单直接交给 `simulator.execute`，中间不再按整手取整：

```253:253:src/a_share_quant/backtest/engine.py
                fills = simulator.execute(orders, day_bars, state)
```

在 `e41e053` 上调用 `generate_orders` 与 `ExecutionSimulator.execute`（成本与滑点均为 0，避免股数被费用干扰）：持仓 `600000` 为 1000 股且当日可卖，现金 0，权益 10000，参考价 10，目标权重 0.5，`lot_size=100`，`volume=2500`，`max_participation_rate=0.10`。`generate_orders` 给出 `sell 500`（目标股数 500，不是清仓）。成交为 `sell 250`，`note=filled`，`capped=True`，余额 750。同一输入下 `300001` 也是委托 500、成交 250、余额 750。R01 的清仓探针（委托卖出 1000）同样成交 250，本条不再依赖那次清仓委托。

250 不是 100 的整数倍。卖出前余额是 1000 股，不属于「余额不足 100 股、应当一次性申报」的情形。买入侧同一上限会收成 200。约束对象是申报数量：持有 1000 股时合法卖出申报只能是 100 股的整数倍，对手方买入申报也是 100 股的整数倍，一笔 500 股的合法卖单不会撮合成 250 股后留下 750 股。这里是把成交股数直接写成 `cap`。

新的可证伪断言：上述输入（持仓 1000 股、无零股、目标减至 500 股、`volume=2500`、参与率 0.10）下，卖出成交股数为 250，余额为 750。该申报数量违反上证函〔2014〕301 号第一条（一）及附件示例一，以及深证会〔2014〕72 号第一条（一）。若成交股数为 200、0，或未截断的 500，则本条不成立。

### F-10

接受。

开盘撮合用成交日全日 `volume` 做上限，是事后容量代理的精度取舍。Claude 已复现：开盘价同为 10 时，`volume=1000` 成交 100 股，`volume=10000000` 成交 99000 股。全日成交量大，上限就宽；全日成交量小，上限就紧。R01 的撤回条件写的就是「若定义是用成交日全日成交量做事后容量筛选，则本条不作为缺陷」。该条件已经满足，并被记为条件成立、只补文档、不改代码。本条到此收口。

---

本文件只涉及审查流程，不构成投资建议。
