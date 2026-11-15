# 2026-11-15 第二轮审计：逐项关闭报告

对 `docs/audits/2026-11-15-u1u2-e1-independent/FINDINGS.md` 的 1 P1 + 8 P2 逐项给出证据。
每一行都必须能被一条命令重复出来；没有生产验收的一律写「未做」，不用「已实现」笼统覆盖五个状态。

| 编号 | 契约 | 代码 | 整链 | UI/CLI 接线 | 生产验收 |
| --- | --- | --- | --- | --- | --- |
| EA-01 写入授权 | S0 §1 | 已提交 | 已提交 | 不适用 | **未做** |
| EA-02 观察预算 | S0 §2 | 已提交 | 已提交 | CLI 文本已加预算行与汇总 | **未做** |
| EA-03 页内容代次 | S0 §3 | 已提交 | 已提交 | 报告文本已跟随 | **未做** |
| EA-04 V2 接线 | S0 §4 | 已提交 | 已提交 | CLI + 界面已接 | **未做** |
| EA-05 集合与续跑 | S0 §6 | 已提交 | 已提交 | batch-plan 只用 plan_batch；BatchPlan.remaining 还没有命令表面 | **未做** |
| EA-06 冻结范围 | S0 §5 | 已提交 | 已提交 | 不适用 | **未做** |
| EA-07 审阅证据 | S0 §7 | 已提交 | 已提交 | 无生产调用方 | **未做** |
| EA-08 范围完整性 | S0 §5 | 已提交 | 已提交 | 覆盖摘要已跟随 | **未做** |
| EA-09 报告范围 | S0 §8 | 已提交 | 已提交 | `batch-report` 已接 | **未做** |

「已提交」指代码、用例与该用例实际跑过的命令都在同一分支里；具体命令与计数见 `.runtime/jpgpng_u1u2_e1_fix_20261115_01/verify/RESULTS.md`。

## 红基线

`docs/audits/2026-11-15-u1u2-e1-independent/evidence/test_boundaries.py` 逐字节未改（每个 lane 各自核对过 sha256）。
修复前 15 项 = 14 失败 / 1 通过；五路合流后整份文件 **15 passed**（verify/merge05_frozen.xml）。

## EA-01（P1）在线备份先写后验

- 契约：S0 §1；本机实测见 `docs/optimization/2026-11-15/evidence/probe_write_locks/`。
- 代码：`core/workspace_ownership.py`（`ParentPin`、`ClaimLease`、`parent_pin()`、`lease()`、`claim()` 的父目录复核与按身份回收）、`core/library_maintenance.py`（`_online_snapshot` 改为 claim → parent_pin → lease → connect/backup/PRAGMA/verify/record，并新增 `stop=`）。
- 为什么不是「再查一次」：claim 只创建并记身份，句柄立刻关闭（所以审计钩子**仍能得手**）；写入能力由 lease 取得——`os.open` 后 `fstat` 比对，符不合即拒绝且不写；句柄保持到 SQLite 写完。实测：持句柄时 `unlink` 拒绝（32）、`os.replace` 拒绝（5）、`unlink+硬链接` 拒绝（32），而 SQLite 仍能打开/写/`journal_mode=DELETE`/`backup` 进入该文件。从 lease 到写完这段，检查与使用是同一个描述符；claim 到 lease 之间仍有窗口，由 lease 的 fstat 比对拒绝（一个字节也不写）兜住，不靠「先 stat 再 connect」。
- 父目录：`CreateFileW`（共享读写、不共享删除、`FILE_FLAG_BACKUP_SEMANTICS`）钉住 + `GetFileInformationByHandle` 与 adopt 的 `_key` 比对；取不到句柄时降级并把 `parent_pin=degraded` 写进备份事实，不假装等价。
- 旁车：只对自己 claim 的名字声明所有权；外部 `-wal/-shm` 保留并以 `unowned` 命名；成功备份后仍有非空旁车 → `snapshot_not_self_contained` 且非成功。
- 证据：`pytest docs/audits/.../test_boundaries.py -k ea01` 2 passed；`tests/test_w1_online_backup_lease.py` 15 例（同一文件在 `a0e5bb5` 基线上 9 红 6 绿）；`tests/test_round2_integration.py` 走公开 CLI 备份→verify→篡改后 verify 失败。
- 用户可见变化：备份进行期间该备份目录被钉住，Explorer 改名/删除会报占用；这是设计取舍，已写进本节。


## EA-02（P2）预算只有阶段标签，没有覆盖实际 I/O

- 契约：S0 §2。
- 代码：`core/decode_limits.py`（新增 `ReadMeter`：读前 clamp 到剩余额度、读后按实际字节计费、逐块查 cancel/时间；`budget.scope` 改为 `file_call`）、`core/quality_check.py`（`_sha256(target, meter)` 分块 256 KiB，额度不足返回 `(None, reason)` 而不是半截摘要；`_receipt_bytes` 不再 `getattr(...,0)`，缺失即 `mark_unknown('decoder_did_not_report_bytes')` 且终态不得成功）、`core/image_validation.py`（结构扫描的 64 KiB 读也走 meter，`ImageValidationStopped(reason)`）、`core/png_decode.py`（每次 read 前 clamp，额度为 0 时 `_Halt(undecided, bytes_limit)`）、`core/quality_evidence.py`（`DecodeReceipt.bytes_read: int|None`）。
- 边界：额度**恰好**用尽且已到文件尾算正常结束（`_reached_end` / `_at_end`），不会把「刚好够」误判成未完成；少 1 字节则不通过——两个方向都有用例。
- CLI 聚合：`decode-check` 的文本输出每个文件打印 `预算: file_call 实测 N 字节 / 上限 M 字节`，并额外打印 `预算汇总: files=N 各文件实测合计=...（每个文件各自一份预算，不是共享总额）`；任一文件读量未知则合计打印「未知」。
- 证据：`-k ea02` 2 failed → 2 passed（修复前 201≠268、取消后仍读满 3 MiB）；`tests/test_w2_budget_accounting.py` 23 例（同一文件放到 `a0e5bb5` 上 18 红 5 绿）；`tests/test_s2_quality_engine.py` 三处 `budget.scope == 'command'` 改为 `file_call`，属于旧断言编码了被否定的语义。
- 遗留：第三方解码器接入时必须回传 `bytes_read`，否则该路径记 unknown + 非成功（这是有意的，不是遗漏）。

## EA-03 页完成必须比对期望内容代次

- 契约：S0 §3。
- 代码：`core/follow_coverage_counts.py`（expected 变成 page_identity→必需代次；`_page_verdict` 五档 delivered/missing/stale/unpinned/unversioned；冲突页 park 成 pending_verify；`delivery_evidence` 四桶相加 == expected）、`core/follow_coverage_state.py`（缺口是「存的是别的代次」时原因写 `pages_content_mismatch`）。
- 证据：`-k ea03` 2 passed（修复前打印 `complete`）；`tests/test_w3_page_versions.py` 24 例；覆盖链回归 257 tests 0 失败。
- 语义收紧：观察有版本、期望缺版本 → unknown（不是 missing，也不是 complete）；期望侧自相矛盾 → 该页 unpinned，不按列表顺序猜最新。
- 注意「四桶相加 == expected」是定义恒等式（delivered = expected − missing − unpinned − unversioned），它证明的是算术不是分区；真正的证据是逐页判定的用例（同版本完成、旧版本不得完成、两版冲突、缺版本 unknown）。

## EA-04 桌面覆盖摘要读的是另一个库

- 契约：S0 §4。
- 代码：`core/follow_facts_v2.py`（一个只读事务读 `library.sqlite3`；期望页来自 `batch_items.required_pages_json`/`page_count`，观察摘要来自 `artifacts.sha256`）、`core/report_commands.py`（`coverage_report_v2`，provider=v2，读取期间文件变化 → `database_changed_during_read`）、`core/collection_desktop.py`（默认 V2、`job_required`、legacy 仅显式）、`core/library_maintenance.py`（`coverage --job` / `--run` 互斥）、`gui/coverage_panel.py` + `gui/collection_controller.py`（任务 ID 可留空＝用当前选中任务）。
- 只读证明：无 `-shm/-wal` 时用 `mode=ro&immutable=1`（连索引文件都不建），有写者时用 `mode=ro` 参与锁；CLI 跑完后库文件 sha256、mtime、目录清单三者不变。
- **重要缺口（不是本轮能闭合的）**：V2 没有保存作品页的期望内容代次。`deliveries.source_identity` 是 `sha256({illust_id,page,url,original,policy_version})` 的引用身份（`core/page_delivery.py:168`），`source_pages.content_digest` 是关注**列表页**的摘要。所以 V2 报告能给出真实的页数与已存数量，但交付维度只能 unknown（`expected_content_version_missing`）、退出码非 0。要闭合必须在采集时把作品页的期望代次写进事实，属于下一轮的数据面工作。
- 证据：`-k ea04` passed；`tests/test_v2_coverage_facts.py` 13 例（含只读模式选择、库被改写时拒绝）、`tests/test_round2_integration.py` 5 例（CLI 备份整链、CLI coverage --job、CLI 只读证明）。

## EA-05 预算裁剪与游标、写回后核验

- 契约：S0 §6。
- 代码：`organizer/batches.py`（OPEN / AWAITING_VERIFY / FINAL 三分类；`resume` 含 applied；`rebuild` 只接受沿 TRANSITIONS 前进；`_status` 单一计算；`plan_batches` 返回 `BatchPlan`，`pool = done ∪ planned ∪ remaining`，容量 0 立即停并保留剩余；新增 `next_action()`，applied 的下一步是 verify，写回结果未知时是 reconcile 而不是重发）。
- 语义变更：applied 从 FINAL 移到 AWAITING_VERIFY；回退码 `progress_would_reopen_finished` → `progress_not_forward`。
- 证据：`-k ea05` 2 passed；`tests/test_w4_batch_plan.py` 30 例；离线链路用例改成「applied 可续、verified 后立刻离开续跑集合」。

## EA-06 冻结范围只冻了一层

- 契约：S0 §5。
- 代码：`organizer/batches.py`（scope/policy/steps 递归冻结，`as_dict()` 返回全新 JSON 结构）、`organizer/group_scope.py`（`__slots__` + 只读属性，`from_dict` 校验 blocks/group_pairs/source/completeness 与指纹一致）。
- 兼容性：新旧指纹与 `Batch.as_dict()` 逐字节相同（W4 用 `git show a0e5bb5` 的旧模块做过 A/B）；`GroupScope.as_dict()` 只多两个 source 键。
- 证据：`-k ea06` 2 passed（`dump['scope']['artist_ids'].append('999')` 不影响原对象；`scope.artist_ids = ('999',)` 抛 AttributeError）。

## EA-07 标签分数冒充看图证据

- 契约：S0 §7。
- 代码：`organizer/rules.py`（`classify` 返回 `match_strength` 与 `visual_evidence`；`confidence=high` 需要两者同时成立；`review_evidence()` 是 `sufficient` 的唯一**生产者**——但 Python 进程内可以手工构造 `VisualEvidence`，所以它是审计入口不是安全边界；它把收据与样本逐项对上：账号/画师/快照绑定、每页 sha256 与 illust_id、`samples.review` 的原文声明、`min(12, available_works)` 个**不同**作品、规则/快照是否过期；纯 dict 只能得到 unverified）。
- 证据：`-k ea07` passed（修复前 confidence=high、needs_more=False）；`tests/test_w5_visual_evidence.py` 22 例。
- 未做：没有生产调用方（本模块尚未接到真实计划写回）；进程内伪造 `VisualEvidence(state='sufficient', receipt_id=..., snapshot_id=...)` 在 Python 里无法阻止，已在代码注释与本表写明。

## EA-08 分组成员矛盾仍标完整

- 契约：S0 §5（scope 完整性）。
- 代码：`organizer/group_scope.py`（memberships 每组 ids 与 artist 行交叉校验；不一致即 partial/unknown 并给 `scope_membership_mismatch:<side>:<group>`；`ensure_current` 区分成员变化与证据降级，后者给 `scope_evidence_degraded`）。
- 用户可见变化：真实快照若本来就 memberships 与 artist 行不一致，覆盖结论会从 complete 变成 partial——这是审计要求的方向。
- 证据：`-k ea08` passed；`tests/test_w4_group_scope.py` 40 例。

## EA-09 报告范围不成立仍退出 0

- 契约：S0 §8。
- 代码：`organizer/report.py`（`covers_batch` 为假即抛 `scope_excludes_batch`；更大的范围需显式 `superset_ok` 并记录 `scope.superset.extra_artists`；snapshot/rules/account 不一致各自类型化拒绝；decision 的 kind↔action↔needs_human 按 `conflicts.ACTION_FOR` 真值表校验；`REPORT_VERSION` 1→2）、`organizer/offline_cli.py`（`--superset-ok/--rules-version/--account`，退出码 0/5/3 分开）、`ORGANIZER_CLI.md` 同提交更新）。
- 证据：`-k ea09` passed（修复前输出 `covers_batch:false` 仍 exit 0）；`tests/test_w5_report_scope.py` 23 例。
- 注意：空批次（0 位画师）的报告也要求显式 `superset_ok`，因为范围确实更大；这是有意的一致口径，不是遗漏。

## 未闭合与不做的部分

1. 生产验收（真实库、真实账号、外置卷、真实整理写回、云盘往返）本轮一律未做。
2. V2 缺作品页期望内容代次（EA-04 的数据缺口，见上）。
3. 旁车所有权模型未建立：`-wal` 只能保留+命名，不能证明归属，因此也没有回收路径。
4. 备份期间目录被钉住是设计取舍；`maintain.py` 尚无 `--stop` 入口（`backup_library(stop=)` 已就绪，接线属下一步）。
5. EA-07 的 `review_evidence()` 目前住在 `organizer/rules.py`，与 `organizer/samples.py` 的规则由测试对齐常量和声明字符串；如需更紧的耦合，应搬到 samples 并由 rules 引用。
