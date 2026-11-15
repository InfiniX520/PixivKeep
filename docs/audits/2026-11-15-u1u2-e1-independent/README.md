# U1/U2 修复、界面接线与 E1 离线引擎独立审计

2026-11-15。冻结产品 **5661d8b**（fix/a1-u1u2-audit-20261115），日常 **4ba891e** 未切换。相对 031627c 共 67 文件、9,998 行新增、474 行删除。本轮审计不改产品，结论不代表真实库迁移、图库归档或账号写回验收。

**结论：有实质进展，但仍有 1 P1 + 8 P2，不建议凭当前绿套件发布。** 上一轮 17 条原始反例全部通过；新增 15 条独立检查有 14 条业务断言失败、1 条正常往返通过。优先处理在线备份写入授权，然后闭合预算、页面内容代次、V2 数据接线和整理范围/审阅证据。

## 阅读入口

- [逐项发现](FINDINGS.md)：触发条件、实际结果、影响边界和修复要求。
- [下一轮开发指导](DEVELOPMENT_GUIDE.md)：S0 契约、文件归属、实施顺序、禁止捷径、验收矩阵。
- [可重放反例](evidence/test_boundaries.py)：小型合成数据，不使用真实图库或账号。
- [机器可读证据](evidence/results.json)：版本、XML 摘要、数量、跳过原因、目录盘点与恢复文件摘要。
- [上一轮审计原文](../2026-11-15-u1u2-independent/README.md)：从 a89842b 的 Git blob 原样补回；其 031627c 结论不能套用到新版本。
- [本轮维护记录](../../maintenance/2026-11-15_jpgpng_u1u2_e1_audit.md)：导航整理与保全情况。

## 独立验证

| 范围 | 总数 | 通过 | 失败/错误 | 跳过 |
| --- | ---: | ---: | ---: | ---: |
| tests 主体（排除 4 个 Tk 模块） | 3314 | 3310 | 0/0 | 4 |
| Tk view / coverage / presentation | 37 | 37 | 0/0 | 0 |
| 现有 tests 套件合计 | **3351** | **3347** | **0/0** | **4** |
| 归档工具独立测试 | 23 | 23 | 0/0 | 0 |
| 11-14 关闭反例 | 14 | 14 | 0/0 | 0 |
| 上轮原始反例，按 a89842b 提取 | 17 | 17 | 0/0 | 0 |
| 本轮新增边界（初跑与打包重放相同） | 15 | 1 | **14/0** | 0 |

以上分组不盲目求和为“唯一测试数”：旧反例可能与 tests 内导入版本语义重叠。完整套件主体 738.917 秒；四个已有跳过的精确原因在 results.json。新增红例是发现证据，尚未修复，不用 xfail 把它们涂绿。

首次 tk_view 只留下 21 个进度点，进程已不存在且无最终 XML；作为未完成尝试保留，不能算通过，也不归因产品。随后新进程 tk_view02 25/25、tk_coverage02 3/3、展示测试逐个进程 9/9。使用已有共享 Python、PYTHONNOUSERSITE=1、PYTEST_DISABLE_PLUGIN_AUTOLOAD=1、pytest -s、显式 TCL_LIBRARY/TK_LIBRARY；此结果不证明 Tk 普遍根因已修。

## 复现

在本审计 checkout 根使用已有共享解释器，下面相对输出路径必须换成新的空目录，不复用历史 basetemp：

```powershell
$env:PYTHONNOUSERSITE='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest docs/audits/2026-11-15-u1u2-e1-independent/evidence/test_boundaries.py -q -s -p no:cacheprovider --basetemp=../recheck_01_tmp --junitxml=../recheck_01.xml
```

默认从证据文件推导当前 checkout；可用 PIXIVKEEP_AUDIT_REPO 显式指定别的冻结树。测试包含临时硬链接与父目录替换，但只操作 pytest 临时目录，绝不能改成真实库路径。原始 runtime 中 run_baseline.py、resume_validation.py、probes01、packaged02 保留命令和运行记录。

## 范围和限制

主审阅读新增/修改核心链路，使用当前 checkout 的 GitNexus 索引辅助追踪，独立复现并审查断言。没有在本轮重新逐行审遍所有旧模块；通过测试不是“万无一失”证明。本轮没有分派开发子任务、安装依赖或请求真实服务。

目录盘点为 **58 活跃 / 73 归档 / 5 个旧脏树**。相对前次增加 15 个活跃树，含本审计自身；没有丢失前次登记路径。此次整理补导航、恢复缺失历史报告、更新当前状态，不移动或删除旧工作树、业务数据和测试失败证据。实际运行仍从日常 JpgPng 启动，不从 review 启动。
