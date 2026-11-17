# U1/U2 与 E1 修复轮独立审计（944ee13）

**结论：上轮 15 条反例均已通过，但相邻路径仍有 1 P1 + 5 P2；本轮不建议切换日常或宣称全部关闭。** 当前问题集中在 SQLite 文件族保护、质量回执的内容绑定、覆盖事实取数、审阅和账号来源，而非要求重做已有效的修复。

- [逐项发现与复现条件](FINDINGS.md)
- [下一轮开发指导、文件分工和验收门槛](DEVELOPMENT_GUIDE.md)
- [机器可读结果与 XML SHA-256](evidence/results.json)
- [13 条独立边界检查](evidence/test_adjacent.py)
- [本轮维护记录](../../maintenance/2026-11-15_jpgpng_u1u2_e1_r2_independent_audit.md)

## 版本和范围

| 角色 | 固定版本 / 分支 |
| --- | --- |
| 被审候选 | `944ee13ea864c47f8ea068292b4d4205d4fa2315`，`fix/a1-u1u2-e1-audit-20261115` |
| 比较起点 | 上轮审计 `75f6ad6`；本轮修复差异 51 文件、6,197 增/340 删 |
| 本审计 | `audit/u1u2-e1-r2-independent-20261115`；只加报告、合成反例和进度导航 |
| 日常入口 | `daily/u1a-w5-w6-20261114` @ `4ba891e`，未切换 |

审查以源码、真实 SQLite/文件系统合成测试和冻结旧反例为准，不把作者自报或测试数作为正确性的替代证明。代码图索引专用于本审计工作树；没有借用日常树的旧索引推断调用关系。没有修改产品源码、真实库、下载文件或账号，没有安装依赖、部署、合并 main 或执行网站写回。仅为核对 SQLite 行为读取官方文档。

## 结果

| 检查 | 总数 | 通过 | 失败 / 错误 | 跳过 |
| --- | ---: | ---: | ---: | ---: |
| 现有 tests 主体，排除六个 Tk 模块 | 3424 | 3420 | 0 / 0 | 4 |
| 四个 Tk 模块 | 118 | 118 | 0 / 0 | 0 |
| 两个 presentation 模块，逐函数独立进程 | 9 | 9 | 0 / 0 | 0 |
| **现有套件合计** | **3551** | **3547** | **0 / 0** | **4** |
| 上轮冻结 EA 反例 | 15 | 15 | 0 / 0 | 0 |
| 更早冻结 UX 反例 | 17 | 17 | 0 / 0 | 0 |
| 归档工具独立测试 | 23 | 23 | 0 / 0 | 0 |
| 历史关闭反例 | 14 | 14 | 0 / 0 | 0 |
| **本轮新增边界** | **13** | **3** | **10 / 0** | **0** |

附加各组独立列出，不混进现有套件总数。作者 fullI.xml 实际是 3419 通过/5 跳过，加 Tk/presentation 后为 3546/5；原文将 total 当 passed。本轮明确设置已入库的归档 collector 路径，多执行了一条原来跳过的用例，因此是 3547/4。剩余 4 跳过为 3 条真实 symlink 环境限制、1 条历史 W 阶段登记。Tk 本轮所有进程直接通过；这不能反向证明历史初始化错误的根因。

## 发现概要

| 编号 | 等级 | 可观察的问题 |
| --- | --- | --- |
| RA-01 | P1 | 在线备份删除 claim 后出现的外来 `-journal/-wal`，仍报成功 |
| RA-02 | P2 | 只读 WAL 查询新建 SHM；无旁车不足以证明 immutable |
| RA-03 | P2 | 尚未访问的画师消失、续跑重复计数、跨任务已有页遗漏 |
| RA-04 | P2 | 从未保存的 review 字典可 sufficient，v1 审阅被 v2 分类直接接受 |
| RA-05 | P2 | GroupScope 丢账号，报告可混用不同账号的范围和批次 |
| RA-06 | P2 | 内容 A→B→A 时，解码 B 的成功被当作最终 A 的成功 |

V2 缺作品页期望内容代次而返回 unknown 是正确的限制，不列为本轮新缺陷。不能为了让报表变绿，把同一份 observed SHA 同时充当 expected。

## 重放与本地证据

工作区证据目录 `.runtime/jpgpng_u1u2_e1_r2_audit_20261115_01/`，其中 `review/` 是本审计树。原始 `full01.xml`、各 Tk XML、`previous_ea.xml`、`previous_ux.xml`、`archive_tool.xml`、`previous_closure.xml` 与最终 `packaged05.xml` 全部保留。`run_baseline.py` 记录精确测试组织；`baseline_python_sha256.json` 保存开始时 Python 文件摘要；目录与脏树证据在 inventory 文件中。

在本工作树、已有项目 Python 环境中运行（不安装、不触发真实采集）：

```powershell
$env:PYTHONNOUSERSITE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -s -B -X utf8 -m pytest docs/audits/2026-11-15-u1u2-e1-r2-independent/evidence/test_adjacent.py -q -s -p no:cacheprovider
```

冻结候选上预期 10 failed / 3 passed。修复后必须逐项转绿，不能改断言实现“关闭”。运行前确保 pytest 临时目录是新的；全量/Tk 的环境变量和精确命令见本地 `run_baseline.py`，不要用默认发现一次跑全部 GUI 取代已经验证的进程组织。

早期 probes01 中有一个 Store 夹具误用，probes02 中一份坏 PNG 被结构层提前拒绝，均保留原记录且不作为业务缺陷证据。最终 packaged05 修正了构造并逐项走到业务断言；13 条均无设置错误。结果收集脚本首次错误假定 pytest XML 有 failure.type，修正收集器后从同一份 XML 重算，未重新执行或改变产品/反例。

## 整理状态

当前盘点 66 活跃、73 归档、7 个含未提交材料；旧路径没有丢失。5 个旧脏树和新增 w1_baseline、w4_organizer 的内容均保留。w4 的未跟踪测试材料已有 21,417 文件、672,143,484 字节，已建立本地文件摘要；本轮没有删除、移动或重新跑归档。当前导航已区分日常、候选、审计，后续按开发指导先保全引用再可恢复归档。
