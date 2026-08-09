# Research 3 — MongoDB 8.x deltas + 独立量化证据(补 research-2 的两个缺口)
> 第二轮 deep research,**增量**做在 research-2 之上(不重证已确立的机制结论)。
> 内部材料,不随报告外发。
> 统计:5 角度 · 抓 21 源 · 提取 86 断言 · 验证 25 · 确认 22 · 否决 3 · 综合 4 · 103 agent

## 研究目标(只查这五个新角度)
research-2 把机制层做透了(根因、Subset/Nested Sets/Bucket/clustered、覆盖索引胖键存疑、$graphLookup 7.0 限制)。本轮只补它明说的两个洞:**8.x 有没有新东西能救这条扫描**、**有没有任何独立实测数字**。

---

## 已验证结论(对抗验证后)

### [1] 没有任何 8.x 查询引擎特性能加速 get_subtree 的 materialized-path 范围扫描(置信 high,3-0)
- **8.0 Express**:只是点查/等值/单索引的快路径(`EXPRESS_IXSCAN`、`EXPRESS_CLUSTERED_IXSCAN`),走的是 `_id` 点读(IDHACK)路子,无 range / sort / skip 资格 → **对 path 范围扫描不适用**。
- **8.0 block(向量化)processing**:仅限 time-series 集合,到 8.3 都没扩到普通集合。
- **$graphLookup disk-spill 修复(SERVER-23980)**:只在 **8.2.0** 落地,**曾从 8.1 回退**(SERVER-102453)→ 100MB 上限在 7.0 和 8.1 都还在;且即便修了,也只救递归遍历那条替代路,本负载里它仍打不过索引区间扫描。
- **结论**:逐文档 `FETCH` 跨整条 8.x 没变,**修法仍是 schema/index,不是升版本**。
- 源:docs/release-notes/8.0、SERVER-23980、SERVER-102453、blog/block-processing(time-series)

### [2] 根因在 explain 层再确认:非覆盖 IXSCAN → 用 RecordId 触发 FETCH 物化整份 BSON(置信 high,3-0)
- 覆盖查询 = IXSCAN 不带子 FETCH、`totalDocsExamined: 0`;非覆盖必经 FETCH 物化全文档 = document-at-a-time 根因。
- 覆盖索引能否在多 KB summary 键下真 index-only 仍**未量化**(research-2 的 baseline (e) 不变)。
- ⚠️ **被否决(0-3)**:"projection 在物化整文档之后才裁字段" 这个措辞被否 → 论证只靠 covered-vs-FETCH 区分,别用"先物化后投影"的时序说法。
- 源:docs/reference/explain-results、dev.to/mongodb find-lifecycle

### [3] 没有任何独立量化基准测过这些 pattern(置信 high,3-0)
- Subset / Nested Sets / Bucket / clustered 在大树或投影密集范围扫描上,**全网无一篇独立实测**给数字;全是定性(vendor 文档)。
- 看似命中的 IEEE CISTI-2018 论文(8398636)宣称量化测了五种树模型 pattern → **相关性和量化两项都被否(1-2)**,不可引。
- → **唯一拿数字的路是在 bench/db 自己跑实验**。这条把 research-2 的核心保留(无实测)从"我们没查到"升级为"确实不存在"。
- 源:docs nested-sets / subset-pattern、oneuptime subset(2026-01,无数字)、IEEE 8398636(否决)

### [4] PG>Mongo 的独立基准存在但都"跑偏",佐证方向不佐证 ~20x;Atlas/企业版也救不了(置信 high,综合)
- **MDPI BDCC 10(2):66(2026-02)**:PG 比 Mongo 复杂分析查询快 **1.6–15.1x**——但**非索引、默认配置、PG17.5 vs Mongo7.0.14、扁平电商 ≤30万行、8GB 机**(未调优、扁平、小)。
- **GeoInformatica 2020**:PG 几乎全胜——但 spatio-temporal AIS(~1.46亿条),差在**空间索引**,不是 BSON 物化。
- → 两者只证 **PG 读优势的方向**,**不能等同 get_subtree 的 ~20x 量级或机制**;别混用 1.6–15.1x 和 20x。
- **唯一规模化失败轶事**:Bucket Pattern 论坛帖(2020,~5.5年前)——索引大降但 app 频繁更新、~120G 工作集、2 节点分片 → **写重场景,不迁移到 write-once**;Subset/Nested Sets/clustered **无**对应失败报告。
- **Atlas/企业版边界**:列式索引 = **仅 Atlas**;自管 Search/Vector Search 自 **8.2** 可跑(独立 `mongot`、公开预览、需副本集)但服务**相关性检索非范围投影** → 不解 get_subtree;auto-scaling/stream/tiered/federated = 仅 Atlas;in-memory engine/审计 = 仅企业版。**结论:没有任何 Atlas/企业版特性能解 get_subtree。**
- 源:mdpi 2504-2289/10/2/66、springer s10707-020-00407-w、community-edition 页、self-managed search blog、bucket forum 帖

---

## 对报告的影响(已折进 optimization.tex)
1. 新增 §"A newer MongoDB version does not close the gap" —— Express/block/​$graphLookup 三条逐条说明都救不了。
2. §Community 段补精确边界:列式仅 Atlas;8.2 自管 Search 是预览且非范围投影;**无 Atlas/企业特性可解**。
3. §实验段补:外部确无独立量化基准(IEEE 候选被否),PG-vs-Mongo 基准跑偏只证方向 → 实验是拿数字唯一路。
4. $graphLookup 段:8.2-only + 曾从 8.1 回退。

## 仍未决(只能实验答,与 research-2 一致并被本轮强化)
- 覆盖索引在多 KB summary 键下能否 index-only(`totalDocsExamined:0`),还是被索引膨胀吃掉?需 bench/db explain + 索引体积。
- Subset / Nested Sets 在 10M 树上 P95 实测降到多少?无任何公开数。
- 树序 _id + clustered 能否让 36k 子树扫描顺序化、降多少?无独立测量。
- ~20x 差距全归 document-at-a-time,还是 PG 的 index-only-scan/HOT 也贡献一部分可分离量?需受控 A/B。

## 证据等级提醒
- 全部 pattern 级证据仍是**定性**;无任何延迟/工作集/驱逐/索引体积数字。
- 两篇 PG-vs-Mongo 论文**跑偏**(扁平/地理,且 MDPI 未调优 30万行)——只取方向。
- 版本时效:graphLookup 修复 8.2-only(8.1 回退、7.0 无);Express/block 8.0+ 但不适用;自管 Search 8.2+ 仅预览。
- Bucket 轶事写重且陈旧(2-1)。IEEE 量化候选已否(不可引)。"先物化后投影"措辞已否(0-3)。
