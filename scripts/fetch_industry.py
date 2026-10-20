"""把 baostock 的行业分类并进 instruments.parquet。

baostock 的 `query_stock_industry()` 一次返回全市场分类（证监会行业），
但它是**当前分类**，不是历史快照——所以 runner 里会标注"静态分类、
存在前视偏差、仅作参考"。行业变更比例很低，用来做暴露归因可接受，
不能用来做选股信号。

用法：python scripts/fetch_industry.py [--data-dir data/processed]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/processed")
    args = ap.parse_args()
    d = Path(args.data_dir)

    import baostock as bs
    lg = bs.login()
    if str(lg.error_code) != "0":
        raise SystemExit(f"baostock 登录失败：{lg.error_msg}")
    try:
        rs = bs.query_stock_industry()
        rows = []
        while rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
    finally:
        bs.logout()

    df = pd.DataFrame(rows)
    print(f"行业查询返回 {len(df)} 行，字段 {list(df.columns)}")
    if df.empty or "industry" not in df.columns:
        raise SystemExit("没有拿到 industry 列")

    # code 形如 sh.600000 -> 600000
    df["symbol"] = df["code"].str.split(".").str[-1]
    df["industry"] = df["industry"].replace("", pd.NA)

    inst_path = d / "instruments.parquet"
    inst = pd.read_parquet(inst_path)
    inst["symbol"] = inst["symbol"].astype(str)
    before = len(inst)
    merged = inst.merge(df[["symbol", "industry"]].drop_duplicates("symbol"),
                        on="symbol", how="left")
    if len(merged) != before:
        raise SystemExit(f"合并后行数变了：{before} -> {len(merged)}（行业表有重复代码）")

    have = merged["industry"].notna().sum()
    print(f"instruments {before} 行，匹配到行业 {have} 行（{have/before:.1%}）")
    print("\n行业分布（前 12）：")
    print(merged["industry"].value_counts().head(12).to_string())

    merged.to_parquet(inst_path, index=False)
    print(f"\n已写回 {inst_path}")


if __name__ == "__main__":
    main()
