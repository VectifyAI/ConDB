# 内部研究材料(讨论准备用)

⚠️ **本目录是内部工作材料,不属于发给 MongoDB 的报告交付物**(交付物是上一级的 `report.tex` / `report.pdf`)。

四份文件:

| 文件 | 内容 | 性质 |
|---|---|---|
| `mongodb_notes.md` | MongoDB 在本基准里**为什么输/赢**、各引擎对比、讨论用一句话弹药 | 我的提炼笔记 |
| `mongodb_optimizations.md` | **优化手段 × 挑战矩阵** + "MongoDB 是不是错工具"正反裁决 + 推荐组合 | 我的提炼笔记 |
| `research-1-missing-dimensions.md` | deep research 完整产出:报告还缺哪些基准维度(冷缓存、$lookup、覆盖索引可行性等),含逐条引用 | 原始研究(对抗验证后) |
| `research-2-optimization-techniques.md` | deep research 完整产出:11 类优化技术的机制/代价/失效场景,含逐条引用 | 原始研究(对抗验证后) |

**读法**:先看两份 `mongodb_*.md` 提炼(结论 + 推荐),要追引用/原始证据再翻两份 `research-*.md`。

**核心结论(三句话)**:
1. get_subtree 视图只需 node_id+title+summary、**不需要 text**,所以拆 text(Subset Pattern)对热路径零代价、直击根因;
2. write-once 让 Nested Sets 的重编号、Bucket 的重写代价都只在 ingest 付一次 → 推荐 **Subset Pattern + Nested Sets/树序 _id**;
3. 全部分析建立在 MongoDB 官方文档的**机制**上,**无任何实测数字**证明能把 P95 从 2.5s 拉下来 —— 唯一缺口,只能靠在 `bench/db` 跑实验补上。
