# 2026-11-17 7e657cb / R3 修复独立复审

## 范围与保全

- 被审候选：`fix/a1-ra-20261117` @ `7e657cbb8ae1092f5c8aa93f15f2ec0925ac0ec7`；本地提交与已登记的远端分支一致，本轮未重新访问远端。
- 审计分支：`audit/ra-r3-followup-20261117`，从被审提交独立建立。
- 日常 `JpgPng` 保持 `daily/u1a-w5-w6-20261114` @ `4ba891e34c312f049ef933eeb517da1e928b7575`，干净、未切换、未部署。
- 未改产品实现、真实库、图库、账号、凭据或业务资料；未联网、安装、推送、合并 main、上传、释放空间或执行真实恢复。

## 结论

R3 的原始 RA-01、RA-03、RA-04、RA-06 修复成立；RA-02 与 RA-05 各发现一个相邻边界，需要小范围 R4。RA-04 的 12 绿 1 红裁决合理，不应为了追绿重新信任未落库字典。

1. **R4-01 P2**：私有副本重试沿用旧成员清单，漏掉第一趟后新出现的 WAL 提交，却标 `copy.verified=true`。
2. **R4-02 P2**：`ensure_current` 从无账号旧 scope 和相同成员集推导新账号，API 报告可由 unknown 变 complete；当前 CLI 未接该函数，不夸大为已发生账号写回。
3. **验证基础设施**：`run_round.py` 不传播子 pytest 失败，自动化可能在 XML 有红时收到退出 0。
4. **P3 文案**：源旁车名字在运行期间出现不等于排他的创建归属，保留字段、收窄解释，不擅自删除。

详见[复审结论](../audits/2026-11-17-ra-r3-followup/README.md)、[逐项发现](../audits/2026-11-17-ra-r3-followup/FINDINGS.md)与[开发指导](../audits/2026-11-17-ra-r3-followup/DEVELOPMENT_GUIDE.md)。

## 独立验证

| 检查 | 结果 |
| --- | --- |
| 主体 | 3558 项，3553 通过、5 跳过、0 失败/错误 |
| Tk 四模块 | 118/118 |
| presentation 两模块 | 9/9 |
| 旧冻结反例 | 15/15 |
| 旧 13 条相邻反例 | 12 通过、1 条具名过期前提失败、0 设置错误 |
| 十一组新增 RA 模块 | 132/132（主体子集，不重复累加） |
| 本轮新反例与对照 | 4 项，2 红、2 对照绿、0 设置错误 |
| 编译 `core gui organizer utils maintain.py` | 通过 |

现有套件合计 3685 项，3680 通过、5 跳过、0 失败/错误。旧/新反例另列，不能把这个数字写成全部反例通过。完整 XML、SHA-256 和命令锚点见[结果索引](../audits/2026-11-17-ra-r3-followup/evidence/results.json)。

## 证据与目录整理

- 正式报告、最终反例和 JUnit XML 已纳入审计分支 `docs/audits/2026-11-17-ra-r3-followup/`。
- 工作区原始证据保留在 `.runtime/jpgpng_ra_r3_audit_20261117_01/evidence/`；早期探索 XML 不作为结论依据，不覆盖实现方 `final2_*`。
- 审计工作树登记在 `.runtime/jpgpng_ra_fix_20261117_01/integration_worktree/.agents/worktrees/audit/ra-r3-followup-20261117`。首次移出嵌套目录被 Windows 拒绝后，保留原 Git 登记，不反复搬迁。
- 未移动、重置、清理或删除任何历史 lane、dirty worktree 或归档；`docs/audits/**` 旧文件、`tests/test_boundaries.py` 和实现树均保持原样。
- 当前导航、`TASK_PLAN.json`、`LOG.json` 只在本审计分支更新，历史关闭报告不改写。

## 下一步

按 R4-A → R4-B → R4-C 小步修复并做红→绿与全量回归。完成后再单独规划统一只读入口、审阅/计划规则来源与 V2 期望内容代次；不在小修中夹带 schema 迁移或生产部署。
