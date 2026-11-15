# U1/U2 接线与 E1 独立审计维护记录（2026-11-15）

本轮从候选 5661d8b 建立 audit/u1u2-e1-independent-20261115，固定比较起点 031627c；日常 4ba891e 保持。只读产品审计、合成测试与文档整理；没有安装、真实数据访问、产品联网、main 合并或部署。

结果见 [完整审计](../audits/2026-11-15-u1u2-e1-independent/README.md) 与 [开发指导](../audits/2026-11-15-u1u2-e1-independent/DEVELOPMENT_GUIDE.md)：1 P1 + 8 P2；现有套件 3347 通过/4 跳过；新增 14 红/1 绿；上轮原始 17/17。首次 Tk 运行未形成最终证据，保留日志并用新进程补跑，结果未混算。

候选祖先没有包含上一轮 a89842b 审计提交；本轮按 Git blob 原样恢复其 5 个报告/证据文件，摘要登记在 results.json，未改写原结论。修正 docs/README 的失效 AMD2 重复目录链接；更新 docs/README、IMPLEMENTATION 页首和 TASK_PLAN.active_workstream；LOG 只追加本轮一条，历史 sessions 不动。

本地 runtime：工作区 .runtime/jpgpng_u1u2_e1_audit_20261115_01/。58 个活跃、73 个归档工作树；5 个既有脏树完整保留。相对前次盘点无路径消失；新增来自实现集成树、开发 lane 与本审计。原始日志、XML、临时 DB 留在 runtime，公开仓库只提交小型合成反例与去敏摘要。

外层 docs/CURRENT_WORKSPACE.md、.runtime/README.md 更新当前入口，修改前原件保存在本批 navigation_before；新增当前候选批次 README。没有重置、移动或删除文件树，没有覆盖历史测试目录。后续物理归档应先用已版本化工具盘点、做可恢复计划，不能依据目录多就清空。

下一步先冻结写入能力/预算/页代次/范围/审阅契约，按指导分包修复，保留原始反例，再独立复审。修复完成与切换日常/生产验收分开记录。
