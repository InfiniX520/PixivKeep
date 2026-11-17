# 7e657cb / R3 六包修复独立复审

日期：2026-11-17  
被审提交：`7e657cbb8ae1092f5c8aa93f15f2ec0925ac0ec7`  
被审分支：`fix/a1-ra-20261117`  
上一审计点：`5d1e676c539209fb517fe9e3b5025baac3c68582`  
审计分支：`audit/ra-r3-followup-20261117`

## 结论

**R3 的大部分修复有效，但当前候选需要小范围 R4 修正，不建议把“六项全部关闭”作为验收结论。**

原始 RA-01、RA-03、RA-04、RA-06 的反例修复成立；RA-02 的静态 WAL 场景成立，但私有副本重试存在新确认的漏提交窗口；RA-05 的直接账号比较成立，但 `ensure_current` 会把无账号的旧范围静默补成当前快照账号。

本轮确认两项实质问题：

| 编号 | 级别 | 结论 |
|---|---|---|
| R4-01 | P2 | WAL 私有副本第二次尝试沿用第一次的成员清单，漏掉新出现的 WAL；报告却写 `copy.verified=true` |
| R4-02 | P2 | 旧 scope 无账号时，`ensure_current` 仅凭成员集相同就取得另一个快照的账号；API 报告可由 unknown 变为 complete |

另有一项验证基础设施问题和一项措辞修正：

- `verify/run_round.py` 不传播子测试退出码，因此相邻反例 1 红时脚本自身仍返回 0；
- `RESULTS_R3.md` 页首的“矩阵跑在 dfe2554”是旧记录；XML 时间与哈希支持 `final2_body/tk/pres` 在 `2aa18c3` 后运行，不应误报成最终全量漏掉后两修；
- “源旁车由本次读取创建”只能证明运行期间出现，不能证明排他的因果关系，应改成“运行期间出现，归属未证明”。

完整证据见 [FINDINGS.md](FINDINGS.md)，下一轮按 [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) 执行。

## RA-04 的 12 绿 1 红裁决

实现方的裁决**成立**。旧反例 #1 在没有 canonical review 的 Store 上先要求 `sufficient`，旧反例 #2 对同类输入要求不得 sufficient；按 R3 §5 的 canonical 门禁，两者不能同时通过。

正确做法不是让未落库字典重新获得信任，而是：

1. 保留原 13 条文件逐字节不动，作为历史红基线；
2. 原反例 #1 仍记为 **1 条过期前提导致的失败**，不写成通过；
3. 由 `tests/test_ra04_canonical_review.py` 中有 canonical review、带规则指纹的 v1→v2 与同版本不同指纹用例承接真正目标；
4. 当前回归选择可 deselect 这条过期前提，同时必须另跑替代用例；不能单纯过滤掉红测试后宣称 13/13。

## 六项状态

| 原项 | 原始反例 | 本轮判断 |
|---|---|---|
| RA-01 | 3/3，原两种删除情形已消失 | 原始缺陷已修；同用户竞争窗口仍是明示边界，源旁车归因文案应收窄 |
| RA-02 | 原 WAL 无 SHM 反例通过 | **部分成立**；R4-01 使 `copy_verified` 不能覆盖重试窗口 |
| RA-03 | 3/3；快照空画师、续跑去重、跨 job artifact 均改善 | 原始缺陷已修；跨 job pending 链接的对照也通过 |
| RA-04 | 未落库字典被拒；canonical v1→v2 有完整替代用例 | 契约与裁决成立；审阅命令规则来源、`plans.py` 消费门禁仍未闭合 |
| RA-05 | 不同账号 scope/batch 直接拒绝，CLI 未记录账号退出 5 | **部分成立**；R4-02 是公开 API 缺陷，当前 CLI 尚未调用 `ensure_current` |
| RA-06 | A→B→A 被降级，解码字节先计费，CLI 要求内容绑定 | 原始缺陷及后补预算问题已修；不扩展成所有格式解码能力已具备 |

## 独立验证

解释器：`D:\Miniconda3\envs\JuiLiu\python.exe`。  
环境：`PYTHONNOUSERSITE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`。  
参数：`-s -B -X utf8 -m pytest -p no:cacheprovider -q`，新 `--basetemp`，保留 JUnit XML。

| 检查 | 独立结果 | 证据 |
|---|---:|---|
| 主体，排除 6 个指定 Tk/presentation 模块 | 3558 项；3553 通过、5 跳过、0 失败/错误 | [body.xml](evidence/body.xml) |
| Tk 四模块 | 118/118 | [tk.xml](evidence/tk.xml) |
| presentation 两模块 | 9/9 | [pres.xml](evidence/pres.xml) |
| 旧 15 条冻结反例 | 15/15 | [frozen.xml](evidence/frozen.xml) |
| 旧 13 条相邻反例 | 12 通过、1 预期失败、0 设置错误 | [adjacent.xml](evidence/adjacent.xml) |
| 十一组新增 RA 模块 | 132/132；它们是主体子集，不重复加到总数 | [targeted.xml](evidence/targeted.xml) |
| 本轮收敛后的新反例与对照 | 4 项；2 失败、2 对照通过、0 设置错误 | [findings.xml](evidence/findings.xml) |
| `compileall core gui organizer utils maintain.py` | 通过 | 命令记录见结果 JSON |

现有套件合计仍为 **3685 项，3680 通过、5 跳过、0 失败/错误**。旧冻结 15、旧相邻 13、新反例 4 都另列，不把这个合计写成“全部反例通过”。

XML 完整 SHA-256、时间及提交锚点见 [results.json](evidence/results.json)。新反例源码为 [test_r4_findings.py](evidence/test_r4_findings.py)。

## 影响面与安全边界

本地 GitNexus 在审计提交上重新建立索引：14,450 节点、37,125 边、300 条执行流。相对 `5d1e676`，差异映射到 41 文件、487 符号、17 条执行流，工具评级为 CRITICAL 影响面；这表示验证范围必须广，不表示存在 487 个缺陷。

- 本轮没有改产品实现，只在审计分支新增报告、反例与导航；
- 没有访问真实库、图库、账号、凭据或业务上传目录；
- 没有联网、安装依赖、推送、合并 main、部署或真实恢复；
- 日常 `JpgPng` 仍在 `daily/u1a-w5-w6-20261114` @ `4ba891e34c312f049ef933eeb517da1e928b7575`，干净；
- 实现树与历史报告未改，旧 `final2_*` 未覆盖；
- 生产、真实账号写回、归档上传/释放、外置卷与云盘往返仍未验收。

## 下一步

先做 R4-A（WAL 副本重试）与 R4-B（禁止隐式账号补全），再做 R4-C（验证脚本与证据口径）。三者修完并重跑全矩阵后，再单独规划统一只读入口与审阅规则来源。不要在这次小修里混入 V2 schema 迁移或生产部署。
