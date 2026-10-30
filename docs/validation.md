# 复现验证记录

验证日期：2026-09-08。Windows，Python 3.14.3；实际使用已有pythonlearning1研究环境，未使用缺少pandas的JupyterProject虚拟环境。依赖版本见requirements.txt。

| Notebook | 顺序执行代码单元 | 导出图 | 结果 |
|---|---:|---:|---|
| 01_single_stock_and_small_universe_prototype | 49 | 1 | 通过 |
| 02_download_and_cache_csi300 | 22 | 0 | 通过，使用冻结缓存 |
| 03_csi300_momentum_factor_research | 62 | 5 | 通过 |

每个notebook由独立Python进程、独立IPython命名空间按原单元顺序执行，无前一notebook遗留变量。执行器捕获文本和图像输出，保存到整理版notebook。修正IPython只读历史库退出提示后重新执行。此验证覆盖代码顺序执行，不等同于在Jupyter网页界面中执行；没有从空环境重新安装依赖，也没有重新发起全量联网下载。

核心20日指标与原notebook已保存输出一致：Mean RankIC=-0.01518120627965321，RankICIR=-0.0718476240800046，年化均值收益=0.042860515604586036，Sharpe=0.19788420268941287，最大回撤=-0.38689174430275985。5/10/20/60日稳健性及三档成本表与原显示结果一致。原Annual Return不是CAGR，README补充几何年化约1.96%，没有替换原指标。

完整读取Parquet得到416388行、300股票、9列，字段缺失为0，code-date重复为0。CSV为300行9列。读取完整文件并计算SHA-256；原桌面文件及archive副本与清单一致；执行前后冻结数据哈希不变。

Git提交范围检查排除Parquet、archive和虚拟环境。notebook输出中的本机项目路径统一显示为<PROJECT_ROOT>。原始输出不做清理，仍留在原件及本地归档中。

局限详见README和file_inventory.md；本次保留原统计研究口径，未将其改写为新交易策略。

## 2026-10-29 补充验证

新增历史股票池、回溯性时间外验证与可执行长仓检验，代码、数据覆盖和结果复核见 `docs/advanced_validation.md`。该区间在原项目和 Git 历史中已经存在，不称为事先冻结的锁定 OOS。新增流程与上述初始 notebook 验证相互独立；正式研究结论以 `data/processed/advanced/` 为准。
