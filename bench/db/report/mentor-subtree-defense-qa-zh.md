# `get_subtree` 汇报：Mentor 追问与回答

这份材料不是让你逐条念，而是用于答辩。回答时按这个顺序：

1. 先用一句话直接回答；
2. 马上限定“这个结论覆盖哪一段”；
3. 必要时再报数字；
4. 没测过的直接说没测，并说明下一步怎么测。

## 先背住：三个最危险的问题

### 1. “你现在只返回 node ID，靠什么重新构成一棵树？”

一句话：只靠 node ID 构不成树，所以现在的 93.192 ms 只是 ID scan，不是可展示的 subtree。

展开：Formatter 至少还需要 `parent_id`、稳定顺序、title，通常也需要 depth 和 summary。完整实现有三种选择：

- 扫 ID 后再读很小的 Structure documents，拿 `parent_id/depth`；
- 把真正需要的 Structure 字段也放进 covering index；
- 换一种能直接恢复树序和父子关系的编码。

无论选哪种，index 会更宽或查询会重新 `FETCH`，必须重新测。当前结果只证明“从 index 返回 IDs”可以优化。

### 2. “真实 API 明明有 depth，为什么你扫了所有 descendants？”

一句话：这轮为了隔离 path range scan，没有加 depth；因此它是大子树压力实验，不是默认 `depth=2` 的真实 endpoint。

展开：当前产品实现会先找到 root depth，然后限制：

```text
node.depth <= root.depth + max_depth
```

本轮 benchmark 没有这个条件，且采样的是较浅节点，所以平均返回 36,456.7 个 IDs。对 `depth=1/2/3` 的交互式 view，这可能偏大；对整树统计、token counting 或深层 utility，它又可能有实际意义。下一轮必须按真实 depth 分布测试。

更关键的是，当前 `(path,node_id)` index 不包含 depth。把 depth 条件加回来后，为了继续 index-only，可能需要把 depth 放进 index；但 path 已经是 range 字段，depth 主要用于 index 内过滤，不一定能缩小扫描区间。必须看真实 explain，不能先说仍然 covered。

### 3. “当前 formatter 本来就要所有节点的 title/summary，为什么还要把 Metadata 拆出去？”

一句话：现在没有证据证明 Metadata 应该单独拆；两集合是必须认真比较的强基线。

展开：当前 `TreeFormatter.format_view` 会使用每个返回节点的 title，并可使用 summary。因此，只要一次 endpoint 返回很多节点，全节点 Metadata 路径就很相关，不能轻描淡写成完全不现实的极端情况。

它被称为 stress variant，是因为当前测的是“无 depth 限制的 36k descendants”，而且还没有 root、排序、merge 和 formatter；不代表每个真实调用都要这么多节点。

如果常见请求给几乎所有返回节点补 title/summary，那么更自然的候选可能是：

```text
Structure + Metadata 放在小文档里
Text 单独拆出去
```

只有当多数请求只要 Structure，或者只给很少一部分节点补 Metadata 时，三个集合才更可能占优。

---

## 第一组：最可能被问的核心结论

### 4. “你这次到底测了什么？”

测的是四个直接存储操作，以及 subtree 的重点拆解。真正深入研究的是：已经知道 path 后，用 materialized-path range 找到所有 descendants，并把 node IDs 拉回客户端。

它不是 semantic search、向量检索或全文搜索，也不是完整 `get_subtree(tree_id,node_id,depth)`。

### 5. “完整 `get_subtree` 还差哪些步骤？”

至少还差：

```text
(tree_id,node_id) 找 root
→ 取得 root path/depth
→ 按 tree_id 和 max_depth 扫 descendants
→ 加入 root
→ 取 parent_id/title/summary
→ 保持顺序
→ merge 并重建树
→ formatter / JSON serialization
→ 按调用方需要读取部分或全部 Text
```

所以当前数字是关键 component，不是 API 总延迟。

### 6. “MongoDB 的 subtree 到底比 PostgreSQL 慢吗？”

准确回答：在 Table 4 对齐的 Structure descendant-ID scan 上，MongoDB 比 PostgreSQL 慢。

- MongoDB：P50/P95 `1.442/11.913 ms`；
- PostgreSQL：`0.398/3.479 ms`。

不能把这句话扩大成“完整 MongoDB `get_subtree` 慢同样倍数”。

### 7. “这个比较公平吗？”

Table 4 对它所测的 component 是公平的：同一台机器、相同 3M 数据、相同 200 条 path、相同五个 Structure 字段、相同 ID 输出、平均都返回 8,840.4 个 IDs，而且两边都确认 index-only/covered。

公平的是 Structure ID retrieval，不是完整 endpoint，也不是数据库所有能力的总比较。

### 8. “所以是不是 4 倍？”

不是统一的 4 倍。本轮 matched 结果是：

- P50：约 `3.62×`；
- P95：约 `3.42×`。

现场最好直接报原始毫秒数。旧的 `4.65×/4.07×` 不属于这轮最终同机结果。

### 9. “两秒是什么意思？完整 API 两秒吗？”

不是。`2,009.189 ms` 是一节点一完整文档布局下，“已知 path 后拉回全部 descendant IDs”这一段的 P95。

它不含 root lookup、depth、Metadata、排序和格式化；同时它也不是平均值。对应 P50 是 `19.560 ms`，说明分布长尾很重。

### 10. “MongoDB 慢的根因到底是什么？”

已证明的一部分根因是：大结果 range scan 后，对每个匹配项逐文档 `FETCH`；covering index 可以消除这部分 `FETCH`。

同一 Structure collection、同一 ID 输出的干净对照是：

```text
path index + FETCH   14.552 / 154.826 ms
covering index        8.938 /  93.192 ms
```

P50/P95 都改善约四成。

但这不是全部根因。覆盖以后仍要扫描并传输三万多个 IDs；MongoDB 与 PostgreSQL 剩余的差距可能来自执行器、协议、cursor、BSON/row decoding 等组合，当前没有继续细分到某个唯一 CPU hotspot。

### 11. “471.550 ms 是 Metadata 自己的耗时吗？”

不是。它是完整 variant 的 P95：covered Structure scan 加全部 Metadata resolution。

它包含 Structure scan、约 37 个 `$in` 批次、网络往返、cursor、BSON decode 和 Python 遍历。它还没做顺序 merge、树重建和 formatter。

不能用 `471.550 - 93.192` 得出“Metadata P95”，因为两个 percentile 来自不同完整 variant，percentile 不能这样相减。

### 12. “三集合是不是已经证明更快？”

没有。已经证明的是：

- 大文档与严重 tail 有关联；
- ID projection 做成 covered query 有明确收益；
- 全节点 Metadata fan-out 有明显成本。

还没证明三集合完整 endpoint 优于一集合或两集合。尤其在当前 all-node Metadata 测试里，三集合组合路径 P95 是 `471.550 ms`，而 Metadata 内联、无 Text 的 reference document P95 是 `252.782 ms`。这不代表两集合已经获胜，因为两者仍不是完整 endpoint，但足以说明三集合不能直接宣布胜出。

### 13. “现在能立刻采用什么优化？”

可以继续采用“让 Structure 所需 projection covered、避免扫描时触碰大 Text 文档”这个方向。

不能立刻冻结三个 collection，也不能冻结最终 index 字段，因为真实 endpoint 还需要 depth、parent_id、顺序和 Metadata。

### 14. “为什么单独优化 MongoDB？”

MongoDB 是候选目标，而且 Table 2 暴露了最明显的 subtree tail，所以 Table 5 做 MongoDB 内部原因拆解。

跨数据库公平比较并没有拿“优化 Mongo”对“未优化 PostgreSQL”：Table 4 给 MongoDB、PostgreSQL、SQLite 都建立了相同五字段 Structure store，并让三边都走 covering/index-only plan。

### 15. “Table 2、Table 4、Table 5 分别回答什么？”

- Table 2：一节点一完整记录时，四个直接操作的整体量级；用于发现 subtree 是问题。
- Table 4：对齐 Structure 字段和 covering capability 后的跨引擎公平对比。
- Table 5：MongoDB 内部用不同完整 query variants 拆 `FETCH`、covering 和 Metadata fan-out。

不要跨表直接做减法。Table 4 是 3M、平均 8,840 IDs；Table 5 是 10M、平均 36,457 IDs。

### 16. “你这份报告的真正贡献是什么？”

不是提出了一个已经完成的三集合系统，而是把原来模糊的“MongoDB subtree 慢”拆成三条可验证事实：

1. 公平 Structure scan 下 MongoDB 仍慢于 PostgreSQL；
2. MongoDB 内部 covering index 确实减少 ID scan latency；
3. 如果全节点补 Metadata，成本会转移到第二阶段。

这给下一步 schema 决策提供了边界，而不是提前宣布最终答案。

### 17. “最大未知数是什么？”

一次真实 `get_subtree` 到底返回多少节点，以及其中多少节点必须有 title、summary 和正文。

这个调用分布直接决定：限制 depth/K 是否已经解决问题；两集合是否优于三集合；Metadata fan-out 是偶发压力还是常规成本。

### 18. “现在能不能据此选 PostgreSQL、放弃 MongoDB？”

不能只凭 Table 4 做数据库总决策。它只证明一个 component 上 PostgreSQL 更快。

最终还要看完整 endpoint 是否满足 SLO、并发吞吐、写一致性、运维、分片和存储成本。如果 MongoDB 绝对延迟满足要求，相对倍数不一定决定一切；反过来，如果完整 P95 超预算，也不能因为生态方便而忽略。

---

## 第二组：`get_subtree` 语义和 PageIndex 使用方式

### 19. “`get_subtree` 是 search 吗？”

不是。它是确定性的存储读取：调用方已经知道 tree、node 或 path，数据库按 key/range 取数据。没有关键词、向量相似度或相关性排序。

### 20. “为什么不逐层 `get_children`，一定要 subtree？”

逐层 children 适合交互式一步导航；subtree 适合一次展开几层、tree view、JSON view、token counting 和整块 utility。

逐层查询会产生多次串行请求；range scan 能一次批量取后代。但批量结果必须有合理 depth/K 边界。

### 21. “产品里的 subtree 包含 root 吗？”

当前 SQLite 实现包含。它先查 root，再 `UNION ALL` descendants。

benchmark range 从 `P + '/'` 开始，只包含 descendants，不包含 root。

### 22. “depth 是相对 root 还是绝对深度？”

API 的 max depth 是相对请求 root 的展开深度。实现转换为 `node.depth <= root.depth + max_depth`。

本轮 benchmark 没执行这个条件。

### 23. “path range 为什么写成 `P+'/'` 到 `P+'0'`？”

在当前受控编码和 binary-style collation 下，所有 descendants 都以 `P+'/'` 开头；字符 `/` 排在 `0` 前，所以这些字符串集中落在这个范围内，而 root 自己不在范围内。

PostgreSQL 使用 `C` collation 来对齐这种字节顺序；SQLite 默认 binary ordering，MongoDB collection 使用默认 simple comparison。

生产若允许任意 path 编码、Unicode、分隔符或转义，必须固定编码和 collation 并做 correctness test，不能机械照搬。

### 24. “结果顺序有保证吗？”

当前 endpoint 需要稳定树序，SQLite 实现显式 `ORDER BY path`。

benchmark 没把显式排序和相同输出顺序纳入 contract。虽然 range index 往往按 index 顺序返回，但没有显式 sort 不应当作为 API 保证。Metadata `$in` 更不保证按输入 IDs 返回，完整实现必须 map 后按 Structure 顺序 merge。

### 25. “只查 IDs 有什么实际价值？”

它能隔离 Structure range scan 的成本，也符合“先导航、后决定哪些节点需要详情”的候选流程。

但当前 formatter 不能只靠 IDs 工作，因此 ID-only 只是一个 component benchmark，不能冒充用户可见结果。

### 26. “当前有哪些调用方真的用 subtree？”

当前代码里包括 tree view formatter、JSON formatter、API expand、token counting 和一些 utility。逐层 beam/block 导航更多使用 point、children 和 entity。

所以当前只能说“大结果 subtree 是单次延迟风险”，还不能说它一定占产品总时间最多；需要结合调用频率。

### 27. “正文是不是永远只给最终选中的节点取？”

交互式 top-down retrieval 通常如此，因此 Text 单独存很合理。

但不能说永远。`format_json(with_entities=True)`、token counter 等 utility 可能要求一整个 subtree 的 entities。下一轮需要按调用方分 profile。

### 28. “为什么不直接限制 depth、分页或 K？”

很可能应该。一次把三万多个节点交给用户、LLM 或 formatter，本身就需要产品层解释。

如果调用方只消费几十或几百个节点，限制 depth/K 可能比复杂 schema 更有效。下一轮应把 `depth=1/2/3`、K=几十/几百/几千和全量压力分开测。

### 29. “节点移动会怎样？”

Materialized path 的代价是：移动一个节点时，它所有 descendants 的 path 都要更新，相关索引也要改。

当前 workload 偏 write-once，报告没测 subtree move。如果结构经常改，必须单独评估写放大，甚至重新考虑编码。

### 30. “为什么不用 parent pointer 递归？”

Parent pointer 很适合 `get_children`，但大 subtree 会产生递归遍历或多层 join。Materialized path 把 subtree 变成一个连续 range，适合读多写少。

本轮没有重新做递归方案对照，所以不要说它在所有场景必然更快。

### 31. “能不能缓存或预计算 subtree？”

可以，尤其树接近 immutable 时。可以缓存热门 root 的 bounded-depth view。

代价包括重叠 subtree 的存储放大、更新失效、大对象限制，以及仍要传输大结果。它是候选方案，当前没有测。

---

## 第三组：实验公平性、统计和可复现性

### 32. “Table 2 本身完全公平吗？”

它在数据、逻辑记录和采样上对齐，但不是 payload 完全一致的严格比较。

例如 point lookup 中 PostgreSQL只返回 title/summary，MongoDB 和 SQLite 还返回 start/end；MongoDB projection 默认还带 `_id`。MongoDB subtree 的 `{"node_id":1}` 也没有排除 `_id`。

因此 Table 2 用来找问题；严格跨引擎结论落在 Table 4。

### 33. “Table 4 的 exact index 是什么？”

本轮单树实测：

- MongoDB：`(path,node_id)`，projection 显式 `_id:0`，hint 该 index；
- PostgreSQL：`(path) INCLUDE (node_id)`；
- SQLite：`(path,node_id)`。

它们语法不同，但都能以 path 做 range，并直接从 index 返回 node ID。

Figure 1 的 `{tree_id,path,node_id}` 是生产候选，不是这轮字面实测 index。

### 34. “为什么 PostgreSQL 要 `VACUUM ANALYZE`？”

PostgreSQL 真正做到 Index Only Scan，不只需要 index 里有 node ID，还需要 visibility map 允许它不回 heap。`VACUUM ANALYZE` 用于准备这个状态，并确认 explain 是 Index Only Scan。

这不是给 PostgreSQL 做额外业务优化，而是确保三边都真的执行所声称的 covering/index-only 路径。

### 35. “MongoDB 为什么必须 `_id:0`？”

MongoDB 默认 projection 会带 `_id`。如果 index 里没有 `_id`，即使 path 和 node ID 都在 index 中，也可能无法 covered。

Table 4 显式排除 `_id`；Table 2 没排除，这也是 Table 2 不能当严格 payload match 的原因。

### 36. “使用 hint 公平吗？”

hint 是为了强制每个 Mongo variant 使用指定 access path，保证干净对照：普通 path index 对 covering index。

它回答“这个 access path 的能力如何”，不回答“planner 在所有生产条件下会不会自动选它”。上线前还要测试无 hint 的真实 planner choice。

### 37. “同一批 path 真的是一样的吗？”

脚本用同一数据和 `seed=7`，从相同 internal paths 中确定性抽样，所以逻辑上相同，并且三个引擎的平均返回数相同。

不足是 raw JSON 只保存 aggregate `avg_rows`，没有保存逐 path ID hash。更强的 artifact 应保存每条 path 的 row count、结果 hash 和延迟。

### 38. “为什么只抽 shallow paths？”

脚本优先选择 path depth 不超过 3 的 internal nodes，这会产生较大的 subtree，适合暴露压力和尾部。

它不是生产 trace，明显偏向大结果。真实系统如果多数从深节点展开两层，结果会小得多。

### 39. “P50/P95 到底怎么计算？”

每组把 200 条不同 path 各正式测一次，排序后取 nearest-rank-like index。P50 是中间附近，P95 大约是第 190 条附近。

它不是同一 query 重复 200 次，因此同时混合：不同 subtree cardinality 和运行时 jitter。

### 40. “P95 高到底是大 subtree，还是数据库抖动？”

当前无法完全拆开，因为没有保存每条 path 的 `nReturned` 与 latency 对应关系，也没有把同一 path 重复多次。

下一轮要同时画：`latency vs nReturned`、每千节点耗时、同一路径重复分布。Table 4 用同一路径保证跨引擎方向仍有意义，但不能把所有 tail 都归因于随机抖动。

### 41. “为什么不用 mean 或 P99？”

分布高度偏斜，P50/P95 更容易区分典型和 tail。Raw JSON 有 mean 和 P99。

但 n=200 时 P99 只由最慢的约两条决定，非常不稳定，所以主表不强调它。

### 42. “warm cache 到底怎么做的？”

不是一个配置。脚本只把样本列表前三条 path 各执行一次但不计时，随后正式计时全部 200 条，包括那前三条。

数据库和 OS cache 不清。这不是全量 warmup，也不能说其余 197 条 fully warm；严格说是一个轻量稳态启动协议。

### 43. “这个 warmup 会不会偏？”

会有局限：前三条比其他 path 多执行一次，而且 arms 按固定顺序运行，后面的 arm 可能受前面 cache 状态影响。

每个引擎的 deployed arm 都处于相同相对顺序，所以 Table 4 仍有可比性；但下一轮应做完整 warm pass或独立 cache protocol、随机化 arm order并重复 campaign。

### 44. “只跑一轮，能说显著吗？”

不能说统计显著。每个 variant 是一轮 200 条 path，没有多轮置信区间。

当前数据是描述性、方向性证据，用于定位机制，不是生产 SLA。正式决策前要重复运行、报告 CI，并记录系统负载。

### 45. “为什么 Table 4 是 3M，breakdown 是 10M？”

3M 用于完成三引擎对齐的 Structure experiment；10M 用于放大并拆 MongoDB 大 subtree 问题。

这两组只能各自回答问题。当前证据没有证明 3.4× 比例在 10M 仍完全相同；若需要 scale conclusion，必须做 10M matched run。

### 46. “数据是真实 PageIndex 吗？”

是 PageIndex-shaped synthetic data，不是真实 production trace。

10M tree 的 shape、字段和 leaf text 由生成器产生：最大深度 8、fanout 大约 6--14、随机重复词汇、顺序数字 node IDs。它能稳定做压力测试，但会带来局限：内容压缩率偏乐观，ID/path 比真实 UUID 短，fanout、depth 和热点分布未必真实。

最终必须用真实 PageIndex tree 和访问 trace 复测。

### 47. “同一台机器就等于资源完全公平吗？”

不等于。相同 host 消除了机器差异，但 MongoDB、PostgreSQL 使用各自默认内存/缓存配置，容器没有 CPU 或 memory cap，也没有做等额 buffer tuning。

这轮结论适用于报告中的默认配置。做数据库选型前应记录并对齐现实资源预算，再做 tuned comparison。

### 48. “机器和版本是什么？”

最终报告运行在双路 Intel Xeon Gold 6418H：48 个物理核、96 个硬件线程、1 TiB RAM。MongoDB 7.0.34、PostgreSQL 16.14 运行在 localhost Docker；SQLite 3.37.2 在进程内。

容器没有显式 CPU/memory limit。

### 49. “1 TiB 内存会不会让结果不现实？”

会限制推广范围。这轮主要观察 memory-resident 下的 scan、`FETCH`、decode 和传输，不代表小内存、冷启动或数据大于 RAM 的行为。

小内存时 cache eviction 和 I/O 可能放大差距，必须单独测；不能把本轮结果外推到所有机器。

### 50. “SQLite 为什么这么快？公平吗？”

SQLite 在同一个 Python 进程里，没有 client-server protocol；MongoDB/PostgreSQL 是服务端数据库。

逻辑字段、path 和 covering plan 是对齐的，但运行架构不对齐。所以 SQLite 是当前 embedded baseline/context，主要 server-to-server 结论看 MongoDB 对 PostgreSQL。

### 51. “为什么移除 DuckDB？”

DuckDB 是 columnar engine。即使字段在一张表中，只投影 path/node ID 时本来就只读相关列；“把大行拆成 Structure”不是与 MongoDB `FETCH` 相同的物理干预。

它还和 SQLite一样是 in-process。可以另做相同完整 endpoint 的 embedded comparison，但不适合用来证明 MongoDB 三集合 schema 的收益。

### 52. “为什么没有 concurrency、多租户、冷缓存？”

这轮先隔离单请求机制，所以只报告 single-client、单树、cache 不清的读取。

因此不能得出吞吐、并发扩展、多 agent、multi-tenant、sharded 或 cold-cache 结论。完整 endpoint 和 schema 确定后，这些是第二阶段实验。

### 53. “localhost 结果能推广到远程服务吗？”

不能直接推广。远程 RTT 会提高每次请求成本，尤其会放大约 37 个 Metadata batches 的问题。

远程部署下减少 round trip 可能比本机更重要，需要重新测。

### 54. “结果正确性怎么验证的？”

当前通过相同数据、相同 path 和相同 aggregate `avg_rows` 做基础校验，并检查 explain plan。

还没有逐 path 保存结果 ID hash、顺序 hash或完整 endpoint equivalence。正式公平实验应先做 correctness hash，再计时。

### 55. “能完全复现吗？”

数据、脚本和 raw JSON 都在 workspace，但当前 result JSON 没嵌入完整命令、时间戳、git commit、容器 digest和所有 DB config；当前工作树还有未提交的 harness 变化。

因此内部可以按 runbook 重跑，但外部 artifact provenance 还不够严谨。下一轮应生成 manifest，冻结 commit 和容器镜像 digest。这一点不要硬说已经完美可复现。

---

## 第四组：MongoDB `FETCH`、covering 和 Metadata 细节

### 56. “`FETCH` 到底是什么？是不是把正文传回来了？”

`FETCH` 是 MongoDB 根据 secondary-index 中的 record reference 读取原 BSON document，以取得或过滤 index 中没有的字段。

正文没有传回客户端；客户端 projection 只要 ID 或 view fields。但数据库内部仍然需要定位并读取 document，这对大量大 documents 会放大成本。

### 57. “covering index 是不是不用扫描了？”

不是。它仍然要扫描三万多个 matching index entries，并把三万多个 IDs 传回客户端。

它省掉的是“每个 index entry 再回原 document 一次”。所以复杂度仍然随 `nReturned` 增长，只是单节点成本降低。

### 58. “哪个对照真正证明 covering 有用？”

只有同一 Structure collection 这对是严格控制：

- 同一 documents；
- 同一 path；
- 同一 ID-only projection；
- 只改变 hint/access path。

结果是 `14.552/154.826 → 8.938/93.192 ms`。

### 59. “去掉 Text 后 P95 从 2 秒降到 253 ms，能说 Text 是根因吗？”

只能说大文档与 tail 敏感性相关，不能说是纯因果量化。

原因：baseline 第一行来自另一轮 run；no-text variant 返回 ID/title/summary，而 baseline 返回 IDs；P50 还从 `19.560` 变成 `22.405`，没有改善。最干净因果结论仍然是 Structure `FETCH → covering`。

### 60. “Table 5 每行能不能看成流水线阶段？”

不能。每行是独立的完整 query variant。

所以不能把 row 1、row 2、row 3 相加，也不能拿 percentile 相减得 stage time。真正 stage breakdown 要在同一次 endpoint 内打 timer/trace。

### 61. “只加 covering index，不拆集合行不行？”

对 ID-only scan，完全可能，而且当前 raw 3M fair run 就说明这是一条强基线。

MongoDB 一完整节点记录但 covered 时是 `1.657/13.571 ms`；单独 Structure covered 是 `1.442/11.913 ms`。差距不大。PostgreSQL 和 SQLite 的 covered-vs-separate 也接近。

因此，covering 是已证明的主收益；物理拆 Structure 只带来小幅、尚未端到端证明的额外收益。

### 62. “那为什么还需要单独 Structure collection？”

可能的理由是让热数据和写入边界更清晰、让非 covered 的 topology reads 只碰小 documents、避免正文进入 collection working set，并为不同生命周期/权限做隔离。

但对纯 ID covered scan，原大 document 根本不会被 FETCH，document 大小影响已经很小。是否值得付出额外集合、join和一致性成本，必须由完整 endpoint 证明。

### 63. “Reference covered 和 Structure covered 很接近，说明什么？”

10M breakdown 中：

- no-text reference covered：`9.162/101.417 ms`；
- separate Structure covered：`8.938/93.192 ms`。

它们接近，说明本轮最主要的效果是 projection 被 index 覆盖，而不是 collection 拆分本身产生巨大收益。

### 64. “为什么有约 37 个 Metadata queries？”

平均 36,456.7 个 IDs，每批 1,000 个：`ceil(36456.7/1000) ≈ 37`。

这是约 37 个 application-level `find` calls；每个 cursor 还可能有内部 batch/getMore，因此不能把 37 精确等同于全部 wire messages。

### 65. “为什么 batch size 选 1,000？”

只是固定实验参数，不是调优结论。

下一轮要 sweep 100、500、1,000、5,000 甚至单个大 `$in`，同时记录请求体、返回 bytes、server CPU、cursor batches 和 P95。减少调用数不保证一定更快，因为请求和响应会变大。

### 66. “为什么不用一个大 `$in`？”

可以试。36k IDs 的请求可能仍能放进命令限制，但 response 很大，也会通过 cursor 分批传输。Planner、内存和 decode 成本也可能变化。

结论必须来自 batch-size sweep，不能先假设一次请求最好。

### 67. “为什么不用 `$lookup`，一次在 server 端 join？”

脚本有候选实现，但这次最终 run 显式在 `kv_in` 后停止，`$lookup` 没有测，所以不能说快或慢。

它可能减少 application round trips，但仍要读取几万个 Metadata documents，也可能增加 aggregation CPU/memory。下一轮应与 client `$in`、两集合 inline Metadata同输出比较。

### 68. “Clustered Metadata 测了吗？”

没有。脚本里有候选分支，但最终 report artifact `stop_after=kv_in`，没有运行 clustered 或 `$lookup` variants。

不要把脚本里存在的代码当成已有实验结果。

### 69. “当前 Metadata 查询完成 join 了吗？”

没有。脚本只把 Metadata documents 拉回来并计数，没有按 node ID 建 map、恢复 Structure 顺序、合并字段或重建树。

因此 `42.404/471.550 ms` 还低估了完整 endpoint 的 client-side work。

### 70. “Metadata 的 key 为什么是 `_id=node_id`？多树不会冲突吗？”

这轮脚本先检查 10M synthetic node IDs 全局唯一，所以实验可以这么简化。

生产若 node ID 只在 tree 内唯一，应使用 `(tree_id,node_id)` namespace，例如 compound `_id` 或 unique index。这个生产 key 的大小和性能没有实测。

### 71. “生产 Structure index 应该是什么？”

目前只能说至少需要 `tree_id` equality、path range 和返回所需字段。Figure 的 `{tree_id,path,node_id}` 只覆盖 ID-only scan。

完整 endpoint 若要 index-only 过滤 depth并重建树，还可能需要 depth、parent_id 等字段；index 会更宽。最终字段顺序必须以真实 query、explain、index size和写成本为准，当前不能冻结。

### 72. “为什么不把 title/summary 也塞进 covering index？”

可以测试，但 summary 可能较大，会让每个 index entry 更宽，增加 index working set、存储和写放大。

应公平比较三种方式：宽 covering index；Structure+Metadata 小 document 的 `FETCH`；单独 Metadata key lookup。不能先假定宽 index 或三集合最好。

### 73. “为什么 covered 后 MongoDB 仍比 PostgreSQL 慢三倍多？”

当前只能说在应用可见的 ID retrieval component 上仍有差距。计时包含 driver、协议、cursor、BSON/row decode 和 Python list materialization，不是纯 index CPU。

要进一步回答需要 server profiler/explain execution stats、client CPU、bytes、cursor batch count和固定 cardinality microbenchmark。现在不能把差距归因到某一个 MongoDB 内部模块。

### 74. “Structure scan 到 Metadata 是指针吗？”

不是 DBRef，也不是物理指针。它是应用层 key association：Structure 返回 IDs，再按 key 查 Metadata。

### 75. “正文怎么拿？”

交互式流程中，先用 Structure/Metadata 决定目标节点，再按 `(tree_id,node_id)` 到 Text 点查正文。

本轮三集合 routed Text endpoint 没测；Table 2 的 content fetch 仍是一节点一完整记录布局下的点查观察值。

### 76. “Structure collection 里为什么同时有 plain path 和 covering index？”

Breakdown 为了做控制实验，同时创建两者，并用 hint 强制选择，才能比较 `FETCH` 与 covered。

生产不一定需要保留冗余 plain path index；若只保留 covering index，可以减少 index storage和 cache competition，但这也要重新测写入和其他查询。

### 77. “path 太长、node ID 是 UUID，会怎样？”

Synthetic node ID 是较短的顺序数字，最大深度也受控。真实 UUID 和更深树会放大 path key，增加 comparison、index size和写成本。

当前结果可能对真实长 path 偏乐观。必须用真实 PageIndex 数据验证 key length 分布和 index footprint。

---

## 第五组：写入、一致性和部署风险

### 78. “Table 3 的写入比较公平吗？”

它只是在一节点一完整记录布局上观察 5,000 次 summary update 和 2,000 次 insert。三个引擎 durability 没统一，物理更新也不同：Mongo `$set`、PostgreSQL `jsonb_set`、SQLite column update。

所以不能据此做严格写入排名，也不是三集合 routed write。

### 79. “三集合会增加多少写？”

尚未测。新节点通常至少涉及 Structure、Metadata，叶节点还可能涉及 Text，以及多个 indexes。

一次逻辑写可能变成多个 document writes；要测 latency、throughput、index write amplification和失败恢复。

### 80. “跨三个 collection 怎么保证一致性？”

MongoDB 不会自动提供 foreign key。选择包括多文档 transaction，或者更适合 write-once tree 的版本化发布：先完整构建新 tree version，校验数量/引用，再原子切换可见版本。

必须定义 retry/idempotency 和 orphan cleanup。当前 benchmark 没实现这部分。

### 81. “读到 Structure 但 Metadata 还没写好怎么办？”

需要明确 consistency protocol：同一 transaction、版本字段加 publish marker，或读取时只选择完整发布版本。

如果不解决，三集合的读性能再快也不能上线。

### 82. “树是 write-once，写问题是不是可以忽略？”

不能完全忽略。Write-once 能让离线构建、版本发布和昂贵索引更可接受，也降低 move/update 频率；但 ingest 仍可能失败，增量修订仍存在，三个集合的一致性仍要保证。

### 83. “分片以后会更好吗？”

没测。若 shard key 以 `tree_id` 开头，一棵树的 subtree 可以路由到单 shard，但热门大树可能形成热点；若打散一棵树，又可能产生 scatter-gather。

当前单机结果不能推广到 sharding。

### 84. “replica set、write concern、远程网络测了吗？”

没有。MongoDB 是本机单服务，PostgreSQL 也是本机服务；没有复制和远程 RTT。

生产 absolute latency、写确认和 failover 都需要另测。

### 85. “三集合的存储开销怎么样？”

Breakdown raw `collStats` 有辅助数字，但索引集合、压缩和 Text 范围没有做完整同输出归一化，因此报告没有据此排名。

生产比较应统计 data、所有 indexes、compression、每节点 bytes、ingest/update amplification。尤其 Structure path index 可能比 Structure data 本身更大。

---

## 第六组：下一步应该怎么做

### 86. “下一步第一件事是什么？”

先冻结完整 endpoint contract，而不是继续凭感觉加 index。明确：root 是否包含、max depth、稳定顺序、必须字段、Metadata 范围、Text 范围、K/分页和错误语义。

没有相同输出，就没有公平 end-to-end comparison。

### 87. “下一轮应该比较哪些 schema？”

至少三组：

1. 一 document 一个完整节点；
2. Structure + Metadata 在小 document，Text 单独；
3. Structure、Metadata、Text 三集合。

三组输入、节点集合、字段、顺序和序列化输出必须完全相同。

### 88. “完整 breakdown 应该怎么打点？”

在同一次请求内分别记录：

```text
root lookup
structure scan + depth filter
metadata resolution
structure/metadata merge
ordering
tree reconstruction
format / serialization
selected text fetch
```

同时记录 `nReturned`、returned bytes、keys/docs examined、server time、client CPU、cursor/getMore 次数。不要再用两个 variant 的 percentile 相减代替 stage timer。

### 89. “Metadata 下一步具体测什么？”

测 Metadata 数量曲线，而不是只找一个神奇 batch：

- 0 个；
- 前 K/可见节点；
- 全部节点；
- batch size sweep；
- client `$in`；
- server `$lookup`；
- Metadata inline 的两集合方案；
- 必要时 clustered key-value。

### 90. “结果规模怎么测才有产品意义？”

按真实请求分层：`depth=1/2/3`、深 root 小 subtree、浅 root 大 subtree、K=几十/几百/几千、全量压力。

报告 latency 对 nodes 和 bytes 的曲线，而不是只有一个平均行数。

### 91. “怎样把 P95 做得可信？”

每个固定 workload 多轮重复；保存逐 path latency/cardinality；同一路径重复测 jitter；随机化 arm order；明确 warm/cold protocol；报告置信区间和系统负载。

### 92. “什么时候补 concurrency 和 multi-tenant？”

完整 single-client endpoint 正确并稳定后。并发 workload 应按真实比例混合 point、children、subtree、content，而不是只压 point lookup。

Multi-tenant 要使用真实 `(tree_id,node_id)` key 和 `{tree_id,path,...}` index、多棵不同大小的树及热点分布。

### 93. “什么时候选择三集合？”

只有当真实 trace 表明多数请求只需要 Structure，或只给很少一部分节点补 Metadata；并且完整 endpoint 明显优于两集合，额外一致性和写成本也可接受。

### 94. “什么时候选择两集合？”

如果常见 formatter/API 会给几乎所有返回节点取 title/summary，Structure+Metadata 合并、Text 单独更值得优先验证。

当前 evidence 让两集合成为必测基线，但还不能直接宣布它获胜。

### 95. “什么时候应该改 API，而不是继续优化数据库？”

如果延迟主要随 `nReturned` 和 output bytes 增长，而且调用方也消费不了几万个节点，就优先限制 depth、K、分页或分阶段展开。

数据库优化不能替代不受控的返回规模。

### 96. “最终成功标准是什么？”

至少要同时满足：

- 完整 endpoint 输出和 SQLite reference 完全一致；
- 真实 depth/K 分布下 P95/P99 满足产品 SLO；
- 多轮结果稳定；
- index/storage/write overhead 可接受；
- 三集合若胜出，优势足以覆盖额外一致性和运维复杂度。

### 97. “如果结果证明全量 Metadata 很常见，怎么办？”

优先考虑两集合或 small-document inline Metadata，而不是继续强行三集合。也可以限制结果规模、缓存 bounded views，或测试宽 covering index，但都要同输出测。

### 98. “如果 mentor 要你一句话给当前建议？”

> 先保留 covering Structure scan 和 Text 隔离这个方向，但暂时不要冻结三集合；先把真实 depth、返回节点数和 Metadata 需求测出来，再用完整同输出 endpoint 决定两集合还是三集合。

### 99. “最诚实的最终结论是什么？”

> 当前已经证明 MongoDB 在公平的 Structure ID scan 上慢于 PostgreSQL，也证明避免逐文档 `FETCH` 能降低 MongoDB 的 subtree tail；但只返回 ID 还不能构树，真实 depth 可能改变 covering 条件，而当前 formatter 又需要 Metadata。因此三集合是否优于两集合，仍然是下一轮端到端实验要回答的问题。

---

## 数字速查卡

### Table 2：10M，一节点一完整记录，平均 36,456.7 IDs

| Engine | P50 | P95 |
|---|---:|---:|
| MongoDB | 19.560 ms | 2,009.189 ms |
| PostgreSQL | 9.271 ms | 102.074 ms |
| SQLite | 8.331 ms | 96.971 ms |

### Table 4：3M，matched Structure covering scan，平均 8,840.4 IDs

| Engine | P50 | P95 |
|---|---:|---:|
| MongoDB | 1.442 ms | 11.913 ms |
| PostgreSQL | 0.398 ms | 3.479 ms |
| SQLite | 0.347 ms | 4.586 ms |

### Table 5：MongoDB 10M breakdown

| Variant | P50 | P95 |
|---|---:|---:|
| 完整节点，path index + FETCH | 19.560 ms | 2,009.189 ms |
| 无 Text reference，FETCH，返回 ID/title/summary | 22.405 ms | 252.782 ms |
| 无 Text reference，covered IDs | 9.162 ms | 101.417 ms |
| Structure，path index + FETCH | 14.552 ms | 154.826 ms |
| Structure，covered IDs | 8.938 ms | 93.192 ms |
| Covered Structure + 全节点 Metadata batches | 42.404 ms | 471.550 ms |

---

## 十条红线：绝对不要这样说

1. 不说“完整 MongoDB `get_subtree` P95 是两秒”。
2. 不说“三集合已经证明最好”。
3. 不说“MongoDB 慢 4 倍”而不限定 Table 4 component。
4. 不说“Text 被传回客户端导致慢”；正文没返回，问题是数据库内部 `FETCH` 大 document。
5. 不说“Metadata 自己耗时 471.550 ms”；这是组合 variant。
6. 不说“只查两次数据库”；是两个索引阶段，当前平均约 37 个 Metadata `find` calls。
7. 不说“Structure 到 Metadata 是指针”。
8. 不说“每行是可相加的 breakdown stage”。
9. 不说“所有 200 条 query 都 fully warm”。
10. 不说“当前三集合已经在生产实现”；它是 candidate layout 和 benchmark prototype。

## 证据位置

- 报告：`bench/db/report/report.tex`
- 四个直接读取：`bench/db/bench_databases.py`
- matched Structure scan：`bench/db/bench_fair.py`
- MongoDB Structure/Metadata breakdown：`bench/db/bench_subset_kv.py`
- 当前完整 SQLite subtree：`contextdb/core/storage.py`
- 当前 formatter：`contextdb/retriever/base.py`
- 最终 raw results：`bench/db/runs/report_3eng_20260716/`
