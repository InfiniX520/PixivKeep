# U1-C 五路拆分（质量证据与深解码）

基线：U1-B 合流后的候选。**只在本目录工作，不安装任何解码组件**（Pillow/numpy 未安装，且用户要求不碰共享解释器）。因此本轮"深解码"用标准库真解 PNG，其余格式一律诚实报"能力不可用"。

## 0. 五条子节点的冻结契约（文件不重叠，可同时开工）

```text
C1 core/quality_evidence.py            —— 纯数据模型，无 I/O
   QualityEvidence(source, transport, structure, decode, pages)   # 每项都是 (state, reason)
   QualityEvidence.as_dict() / from_dict()
   DecodeReceipt(format, frames, pixels, limits, decoder, outcome, seconds, checked_at)
   合法取值表：
     source:    declared_original | unknown
     transport: length_matched | unknown
     structure: ok | failed | undecided | unsupported
     decode:    ok | failed | undecided | capability_unavailable
     pages:     all_frames | first_frame_only | unknown

C2 core/png_decode.py                  —— 标准库真解码（不依赖任何第三方）
   decode_png(stream, *, limits, stop=None) -> PngResult(state, reason, width, height, frames, pixels, rows_unfiltered)
   要求：解析 IHDR/IDAT/IEND（含 Adam7 七趟）、zlib 解压、逐行反滤波（None/Sub/Up/Average/Paeth）、
         每行 filter 合法、每趟字节数精确、CRC 校验；失败给 reason（invalid_filter / truncated / bad_crc /
         bad_zlib / unknown_critical_chunk / interlace_mismatch …）。

C3 core/decode_limits.py               —— 上限、预算与取消（纯逻辑）
   DecodeLimits(max_bytes=…, max_pixels=…, max_frames=…, max_seconds=…)
   Budget(limits, *, clock=…).check_bytes/check_pixels/check_frames/check_time() -> None | reason
   Stop = 协议（is_set()）
   超限/取消的返回值必须是 "undecided" + reason，绝不返回 failed。

C4 core/decoder_registry.py            —— 能力门与适配器协议
   Decoder = Protocol(name, formats, decode(path, limits, stop))
   probe_decoder() -> Decoder | None        # 本轮恒 None：没有组件
   capability(format) -> "available" | "unavailable"
   decode_with(registry_like, path, *, policy, limits, stop) -> DecodeOutcome
        policy="strict" 且不可用 -> capability_unavailable（绝不 ok）
        policy="structure" -> 交给结构级，decode 记 "unavailable" 的原因

C5 tests/png_samples.py + tests/test_png_samples.py —— 合成样本构造器（供主线程写跨模块矩阵用）
   提供 build_valid_rgba() / build_interlaced() / build_bad_filter() / build_truncated() /
   build_oversized_header() / build_gif_header() / build_jpeg_header() / build_webp_header()，
   每个样本返回 bytes；测试用 struct/zlib 独立验证"这个样本确实是我声称的那种"（不依赖 C1–C4）。
   跨模块验收矩阵由主线程在合流后用这些构造器写，避免 C5 在并行期依赖尚未合流的模块。
```

## 1. 手工造的合成样本（C5 负责，不下载图片）

用 zlib 现场生成：最小 RGBA PNG、隔行 PNG、故意改坏一行的 filter 字节、截断 IDAT、超宽高 IHDR、GIF89a/JPEG/WebP 头部字节（只需让结构级能识别格式）。每个样本在测试里现场构造，落盘到 pytest tmp_path，测试结束即消失。

## 2. 主线程合流后做

- `core/image_validation.py` 增加 `decode_policy=("structure"|"strict")`（默认 structure，现有 2 处调用点行为不变）；
- ~~`maintain.py scan --decode`~~ → 改为独立 `maintain.py decode-check <文件...>`（默认要求解码，`--structure-only` 退回结构级）。理由见 [U1C_QUALITY_EVIDENCE.md](U1C_QUALITY_EVIDENCE.md) §4：把解码塞进带游标与预算的 scan 会同时改变预算语义并混淆 coverage；扫描接线留到性能那一轮单独评估。
- 文档 `docs/optimization/2026-11-14/U1C_QUALITY_EVIDENCE.md`：五维表、能力门规则、上限与取消语义、**"未安装解码器"的明确声明**。

## 3. 验收

- C1–C4 各自单测全绿；C5 矩阵全绿；
- 解码前后 sha256 一致（只读）；
- 历史 `verification="structure_checked"` 行一个字都不改（有断言）；
- 全量矩阵与三个 Tk 模块按既定口径重跑。

## 4. 之后

U1-C 收口后进 **U2-A**（同样先拆五路），再 **U2-B**。U1 与 U2 全部完成后我才通知你审计。
