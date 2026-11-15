# 2026-11-15 U1/U2 第二轮审计修复记录（EA-01…EA-09）

审计提交 `75f6ad6` 冻结的是产品 `5661d8b`；结论是 1 P1 + 8 P2，并明确「不建议切换日常版本」。
本轮从审计提交开分支 `fix/a1-u1u2-e1-audit-20261115`，按 `S0_CONTRACTS_R2.md` 的八条契约把九项逐条关闭。
日常树 `E:\Zero9\JuiLiu\JpgPng` 全程停在 `4ba891e`，未切换、未部署。

## 做法（为什么不是「照着报告改一遍」）

1. **先冻结契约再动手**：八条契约写清生产者、消费者、未知、矛盾、过期五种情形，并把「本机实测」写进 §0——持句柄能否挡住替换、是否妨碍 SQLite 写入，这是 EA-01 的方案前提，不能靠猜。
2. **五路并发、文件不相交**：W1 写入授权、W2 观察预算、W3 页代次、W4 整理核心、W5 审阅与报告；主智能体只写契约、CLI 聚合、接线与文档，最后合流。
3. **冻结反例先红后绿**：审计的每一项在 `a0e5bb5` 基线上都被复跑成红，再在分支上转绿（ea01/ea03/ea05/ea06/ea08 各有基线 XML）。此外 W1 的 15 条新用例在基线上跑出 **9 红 / 6 绿**，W2 的 23 条跑出 **18 红 / 5 绿**。W3/W4/W5 的新用例只在分支上跑绿，没有整体基线复跑——它们要证明的东西由各自的冻结反例承担，这里不夸大。
4. **冻结红基线逐字节不动**：`docs/audits/2026-11-15-u1u2-e1-independent/evidence/test_boundaries.py` 在五个 lane 和合流后的树上 `git diff` 均为空。

## 逐项结果

| 编号 | 关键改动 | 红→绿 |
| --- | --- | --- |
| EA-01 P1 | `claim()` 不持句柄；`lease()` 按名打开后 fstat 比对并保持到 SQLite 写完；父目录 `CreateFileW` 钉住 + `GetFileInformationByHandle` 比对；`parent_pin=degraded` 如实上报 | ea01 2 failed → 2 passed |
| EA-02 | `ReadMeter` 逐块 clamp+计费+查取消；`_sha256(target, meter)`；`DecodeReceipt.bytes_read`；`budget.scope=file_call`；CLI 补预算行与 `files=N` 汇总 | ea02 2 failed → 2 passed |
| EA-03 | expected 带期望内容代次；五档页判定；冲突即 park；缺代次只能 unknown | ea03 2 failed → 2 passed |
| EA-04 | 新增 `core/follow_facts_v2.py`；摘要默认读 `library.sqlite3` 并标 provider；CLI `--job/--run` 互斥；界面用选中任务 | ea04 failed → passed |
| EA-05 | applied 移出 FINAL；`resume` 含 applied；`BatchPlan` 保集合守恒；`next_action()` 区分 reconcile | ea05 2 failed → 2 passed |
| EA-06 | scope/policy/steps 递归冻结；`GroupScope` 只读；`as_dict()` 全新结构 | ea06 2 failed → 2 passed |
| EA-07 | `match_strength` 与 `visual_evidence` 分离；`review_evidence()` 才给 sufficient | ea07 failed → passed |
| EA-08 | memberships 与 artist 行交叉校验；`ensure_current` 区分成员变化与证据降级 | ea08 failed → passed |
| EA-09 | 范围不覆盖即拒绝；更大范围需 `superset_ok` 并记录；三份输入来源对齐；decision 真值表 | ea09 failed → passed |

## 验收（命令与结果）

解释器 `D:\Miniconda3\envs\JuiLiu\python.exe`，`PYTHONNOUSERSITE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，参数 `-s -B -X utf8 -m pytest -p no:cacheprovider`，每次新 basetemp。

```
冻结红基线（15 项）        15 tests / 0 failures      verify/merge05_frozen.xml
整树（排除 6 个 Tk 模块）  3424 tests / 0 failed / 0 errors / 5 skipped / 758 s   verify/fullG.xml
Tk 四模块                 118 tests / 0 failures      verify/tkF.xml
两个 presentation 模块      9 tests / 1 error → 单条重跑 1 passed（已知 Tk 初始化现象，非产品失败）
```

## 未做与遗留

1. 生产验收全部未做：真实库、真实账号、外置卷、真实整理写回、云盘往返。
2. V2 没有保存作品页的期望内容代次（`deliveries.source_identity` 是引用身份，不是内容摘要），所以覆盖摘要的交付维度只能到 unknown。这是数据缺口。
3. 外部 `-wal/-shm` 没有所有权模型，只能保留并命名，没有回收路径。
4. 备份期间备份目录被钉住；`maintain.py` 尚无 `--stop` 入口（`backup_library(stop=)` 已就绪）。
5. `BatchPlan.remaining` 还没有命令表面（`batch-plan` 仍用 `plan_batch`）。
6. 上一轮遗留：`core/library_maintenance.py` 的巡检汇总里 `stats.get("bytes_hashed", 0)` 会把未测到的字节打印成 0，与本轮「未知不写 0」的口径不一致，单独记一笔。
