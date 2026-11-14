# U1-C 质量证据与深解码（2026-11-14）

基线：U1-B 收口后。五条 lane 合流，本文档记录**能说什么、不能说什么**，以及本机为什么只有 PNG 真解码。

## 1. 五个维度互相不能替代

| 维度 | 取值 | 能说明 | 不能说明 |
| --- | --- | --- | --- |
| source | declared_original / unknown | 这次用的是元数据声明的原图 | 平台没有处理过作者源文件 |
| transport | length_matched / unknown | 实际字节数与记录一致 | 服务器返回的就是目标作品 |
| structure | ok / failed / undecided / unsupported | 容器与数据结构自洽（现有 `inspect_image`） | 像素能被解码 |
| decode | ok / failed / undecided / capability_unavailable | 指定解码器在限制内读完了像素流 | 远端原件真伪 |
| pages | all_frames / first_frame_only / unknown | 页/帧集合是否读完 | 多帧格式的每一帧都被语义校验 |

合成一行：**没有解码就没有 pages=all_frames**。只有 `decode=ok` 时才会写 `all_frames`，否则一律 `unknown`（reason=`decoder_did_not_read_frames`）。

## 2. 本机实际做了什么

- **PNG：真解码**（标准库，无外部组件）：解析 IHDR/PLTE/IDAT/IEND（含 Adam7 七趟）、zlib 解压、逐行反滤波（None/Sub/Up/Average/Paeth）、每行 filter 合法、每趟字节数精确、CRC 校验、未知 critical chunk 拒绝、APNG（acTL）明确不支持。
- **JPEG / GIF / WebP：能力不可用**。本机没有 Pillow/numpy，且用户本轮要求不安装任何包。严格策略下 `decode=capability_unavailable`（reason=`no_decoder_component`），**绝不当 ok**。
- **资源上限**：`max_bytes / max_pixels / max_frames / max_seconds` + stop。超限或取消 → `undecided`（reason=`bytes_limit`/`pixels_limit`/`frames_limit`/`time_limit`/`cancel`），**不是 failed**。
- **只读证明**：`check_file()` 解码前后各算一次 sha256，报告里写 `unchanged` 与前后摘要。

## 3. CLI

```powershell
& $py -s -B -X utf8 maintain.py decode-check <文件> [<文件> ...]            # 默认要求解码
& $py -s -B -X utf8 maintain.py decode-check <文件> --structure-only      # 只做结构级
& $py -s -B -X utf8 maintain.py decode-check <文件> --max-pixels 5000000  # 限制
```

退出码：**0** 每个文件都真的解码通过；**2** 有文件被判定为内容不通过（结构或解码 failed）；**5** 没有全部解码通过（未请求 / 能力不可用 / 超限 / 取消）；**3** 文件读不出来；**4** 参数被拒。

## 4. 为什么没有接进 `scan`

计划里原本写"给 `maintain.py scan` 加 `--decode`"。实际没有这么做，理由：`scan` 的语义是"账本行 vs 磁盘文件"，它有**逐页预算与游标**；把解码塞进去会让同一行在一次扫描里既做哈希又做像素解码，预算含义和耗时都变了，且会让"没解码"与"解码失败"混进同一份 coverage。

所以先给独立的 `decode-check`（单/多文件、明确退出码），把"是否给扫描加解码预算"留到 U2-C 性能那一轮单独评估。这是**有意的范围收缩**，不是遗漏。

## 5. 验收结果

- `core/quality_evidence.py` 13 项、`core/png_decode.py` 24 项、`core/decode_limits.py` 48 项、`core/decoder_registry.py` 15 项、`tests/png_samples.py` 12 项；
- 跨模块矩阵 `tests/test_decode_matrix.py` 13 项；CLI `tests/test_decode_cli.py` 6 项；
- 真实文件冒烟：`docs/refactor/designs/approved_2026-10-21/follow_user.png`（117,640 B，1360×900）→ 结构 ok、解码 ok、1,224,000 像素、用时约 0.72 s、**解码前后 sha256 不变**。

## 6. 诚实列出的限制

1. **没有安装任何解码组件**：所以 JPEG/GIF/WebP 的"能解码"分支从未在本机跑过；只验证了"能力不可用"这一支。
2. C2 的隔行覆盖以 2×2/8×8 为主；16-bit + Adam7、tRNS/sBIT/iCCP 等辅助 chunk 只做"跳过 + CRC 校验"，不做语义校验。
3. `memory_limit` 分支只有代码路径，没有可复现测试（不想靠分配巨内存触发）。
4. 未测试从不可 seek 的管道读取（实现只要求 `read`）。
5. `pages` 维度只表达"读完/没读完"，不表达"每一帧都被语义校验"。
6. 历史 `verification="structure_checked"` 行**一个字都没改**：本批只新增维度，不批量升级旧收据。
7. 结构与解码的失败**不双报**：结构已经 failed 的文件不再解码（reason=`structure_not_ok`），避免"两个 failed"掩盖是谁先发现的。

## 7. 复现

```powershell
$env:PYTHONNOUSERSITE = '1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest tests/test_decode_matrix.py tests/test_decode_cli.py -q
```
