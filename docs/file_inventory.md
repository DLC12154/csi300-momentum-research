# 文件用途、依赖与整理映射

所有源文件逐个读取；数据文件完整读入并检查字段、日期、缺失和重复。原路径不变，整理版本为复制，原始字节和输出另存本地 archive/originals（不提交）。SHA-256见 original_manifest.json。

| 原文件 | 整理版本 | 用途与依赖 |
|---|---|---|
| sample.ipynb | archive/originals/sample.ipynb | PyCharm Hello World 模板，与研究无关，仅归档 |
| notebooks/01_5test.ipynb | notebooks/01_single_stock_and_small_universe_prototype.ipynb | 单股000001及五股票原型；缓存行情/AKShare → 收益/动量 → IC/RankIC |
| notebooks/02_download_csi300.ipynb | notebooks/02_download_and_cache_csi300.ipynb | 成分股CSV → 股票代码 → 后复权腾讯行情 → Parquet；现在优先缓存且拒绝覆盖 |
| notebooks/03_momentum_factor_research.ipynb | notebooks/03_csi300_momentum_factor_research.ipynb | Parquet → 20日动量/未来5条记录收益 → RankIC/ICIR → 分组/回测 → 年份/多窗口/固定成本 |
| data/raw/csi300_constituents.csv | 同名 | 2026-09-04成分股快照，300行9列，冻结股票池 |
| data/raw/csi300_prices_2020_2025.parquet | 同名（仅本地） | 416388行9列，300股票，2020-01-02至2025-12-31；没有缺失和code-date重复 |
| README.md | README.md | 原研究说明归档；新说明根据实际重跑结果生成 |
| requirements.txt | requirements.txt | 原文件为空；新文件记录实际执行环境的直接依赖版本 |

.venv和.idea为环境/编辑器文件，不是研究内容，不复制上传。models目录为空。没有其他研究代码或文本文件。新增src/data.py统一缓存读取；新增src/run_notebooks.py顺序执行并保存输出；data/processed保存小型汇总表，figures保存研究图。

## 改动与回退

移除整理版中的安装/卸载单元、未使用函数；修正空missing_codes索引；加入与工作目录无关的路径定位；保留统计公式。原始源文件从未移动、改名或覆盖。回退只需继续使用原JupyterProject，或根据清单使用archive/originals中的字节一致副本。

## 方法审计

原RankIC均值/标准差是未年化ICIR，标签重叠，因此不能直接当独立样本显著性。多空收益Q5-Q1按单位多头与单位空头名义头寸，是简化差值净值。原最大回撤从第一个观测净值起算，未加入初始1；保留该口径。各回看窗口因起始日不同，调仓相位也不同，并非严格同区间控制实验。年份按信号日期归属，跨年持有期收益不拆分。没有行业/市值中性化、停牌涨跌停约束、真实成交延迟及借券可行性。未来价格可得性参与样本筛选。请勿将此结果描述为可交易alpha证明。
