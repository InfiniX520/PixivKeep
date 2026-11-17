# R3 后续审计证据索引

- 被审产品提交：`7e657cbb8ae1092f5c8aa93f15f2ec0925ac0ec7`。
- 最终红基线：`test_r4_findings.py`，**4 项 = 2 个实质反例失败 + 2 个对照通过**，无设置错误。
- 默认在仓库根目录执行：

```powershell
$env:PYTHONNOUSERSITE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest `
  -p no:cacheprovider -q --basetemp <新的临时目录> `
  --junitxml <新的XML路径> `
  docs/audits/2026-11-17-ra-r3-followup/evidence/test_r4_findings.py
```

测试文件也可通过 `PIXIVKEEP_AUDIT_REPO` 指向另一个检出；对照旧候选时必须确保它确实是要测的提交。

## 文件

| 文件 | 含义 |
|---|---|
| `body.xml` | 主体 3558：3553 通过、5 跳过、0 失败/错误 |
| `tk.xml` | Tk 四模块 118/118 |
| `pres.xml` | presentation 两模块 9/9 |
| `frozen.xml` | 旧冻结 15/15 |
| `adjacent.xml` | 旧相邻 13：12 通过、1 条具名过期前提失败 |
| `targeted.xml` | 十一组 RA 模块 132/132；主体子集，不累加 |
| `findings.xml` | 新反例与对照 4：2 红、2 绿 |
| `results.json` | 命令、提交、计数、退出码、完整 SHA-256 和保全边界 |

完整命令与环境记录在 `results.json`，不要从旧报告的计数推断本批结果。

工作区 `.runtime/jpgpng_ra_r3_audit_20261117_01/evidence/exploration/` 仅保留早期探索与试错，不是验收证据；正式结论只使用本目录。
