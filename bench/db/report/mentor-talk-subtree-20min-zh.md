# PageIndex 的 `get_subtree` 慢在哪里

这版正文按约 20 分钟准备，重点是 subtree。Table 2 讲现象，Table 4 回答公平性，Table 5 拆 MongoDB 的瓶颈。末尾的追问不计入正文时间。

## 0:00--2:00 问题和结论

今天我主要讲 `get_subtree`。其他三个读操作先用一句话带过：在一千万节点上，MongoDB 的 point lookup、expand children 和 fetch content，P95 都低于 0.35 毫秒；真正出现明显长尾的是 subtree。

我想回答三件事：MongoDB 的 subtree 是否真的比 PostgreSQL 慢；慢发生在 path scan、文档 `FETCH`，还是后续 metadata lookup；以及三集合方案现在证明了什么、还没有证明什么。

结论先说。在严格对齐的 structure ID scan 上，MongoDB 的确比 PostgreSQL 慢：P50/P95 是 1.442/11.913 毫秒，PostgreSQL 是 0.398/3.479 毫秒。MongoDB 内部，把 structure scan 从逐文档 `FETCH` 改成 covering index 后，P95 从 154.826 降到 93.192 毫秒。但如果给返回的所有节点都补 title 和 summary，整个组合路径的 P95 是 471.550 毫秒。

所以，这轮已经证明 covering structure scan 有价值；但还没有证明三个 collection 就是完整 endpoint 的最优布局。

## 2:00--5:00 到底测了哪一段 `get_subtree`

这里先把产品 API 和 benchmark 分开。

产品里的 `get_subtree(tree_id, node_id, depth)`，应该先用 `tree_id` 和 `node_id` 找到 root 的 path 和 depth，再取 root 和指定深度内的 descendants，排序后交给 formatter。当前 SQLite 实现就是这条语义。

这次 benchmark 没有把整条链计时。它预先拿到了 path，只执行：

```text
path >= P + "/"  and  path < P + "0"
```

然后只投影 `node_id`，把所有匹配 ID 完整拉回客户端。假设 P 是 `/A/B`，这一步取的是 `/A/B/` 前缀下的全部后代。

因此报告中的 subtree 数字，严格说是 materialized-path descendant-ID range scan。它不含 root lookup，不含 root 本身，没有 depth 截断、排序、分页、metadata、正文和树格式化。

这次还是单树实验，所以实测查询没有 `tree_id` 条件。Figure 1 里的 `{tree_id, path, node_id}` 是多树部署时的候选索引；本轮公平实验实际使用 `(path, node_id)`。我会主动把 design 和 measured index 分开，不能说多树生产索引已经验证。

后面看到 MongoDB P95 两秒时，也要按这个边界理解：它不是完整 `get_subtree` 的两秒，而是其中结果规模最大、最容易出现长尾的 descendant-ID scan。

## 5:00--7:30 为什么 subtree 特别慢

点查通常返回一个节点，children 也只返回少量结果。subtree 的成本则跟后代数量直接相关。10M 数据的 200 条测试 path，平均每条返回 36,456.7 个 IDs。数据库不只是定位到第一个索引项，还要扫描、传输并 materialize 三万多个结果。

一记录一节点的 MongoDB baseline 还有逐文档 `FETCH`。普通 secondary index 只有 path 和 record ID；查询需要的 node ID 不完全在这个 index 里，MongoDB 就要对每个匹配项读取 BSON document，再做 projection。Table 2 的 projection 还默认带 `_id`，因此也不是 covered query。

这些 document 同时包含 structure、title、summary 和可能很大的 leaf text。即使最后只返回 ID，执行时仍会触碰大文档。数据能放进内存，只是减少磁盘 I/O，并不会消除 document lookup、BSON materialization、网络传输和客户端迭代。结果越大，这些成本越容易被放大到尾部。

## 7:30--10:00 三集合改变了什么

候选方案按访问模式拆成三个集合：Structure 只存 tree ID、node ID、parent ID、depth 和 path；Metadata 存 title 和 summary；Text 存正文。

这样 subtree 第一阶段可以只扫较小的 Structure，并用 covering index 直接返回 node IDs，不读取 BSON document，也不会碰正文。

Structure 到 Metadata 不是指针，也不是 `DBRef`，只是 key lookup。生产设计可以用 `(tree_id, node_id)` 关联；当前单树 breakdown 先确认 node ID 全局唯一，再简化成 `_id = node_id`。

从 path 拿 metadata，逻辑上有两个索引阶段：Structure 做 path range scan，再按 node ID 查 Metadata。但这不等于只有两次数据库请求。脚本每 1,000 个 IDs 发一批；平均 36,456.7 个 IDs，就是一次 structure query 加大约 37 次 metadata queries。

正文不随整棵 subtree 读取。等上层最终选中少量 node IDs 后，再直接去 Text 点查正文。

这个设计的合理部分是让 navigation 不碰正文、让 ID scan 可被索引覆盖；但如果调用方需要给三万多个节点全部补 metadata，第二阶段也可能很贵。因此“三集合一定最好”还不是本轮结论。

## 10:00--13:00 为什么 Table 4 是公平比较

Table 2 先提供一记录一节点的整体背景：同一份树、同一批 path、相同逻辑记录。但各 driver 的 projection 有细微差异，MongoDB 默认还返回 `_id`，所以我不会用 Table 2 算一个精确倍数，再把所有差异都归因于数据库引擎。

真正回答公平性的是 Table 4。它使用 3M 节点和相同的 200 条 path，三个数据库平均都返回 8,840.4 个 IDs。三边都有独立的 structure store，字段完全相同：tree ID、node ID、parent ID、depth 和 path；输出都只有 node ID；三边都确认使用 covering access path。

MongoDB explain 是 `PROJECTION_COVERED + IXSCAN`，documents examined 为零。PostgreSQL 是 `Index Only Scan`，SQLite 是 `COVERING INDEX` scan。实验在同一台机器上、同一 seed、同一组 path、单客户端运行。

MongoDB 和 PostgreSQL 都是本机 Docker 服务，所以它们是主要的 server-to-server 对比。SQLite 在进程内运行，没有 client-server protocol 开销，只作为 embedded context，不拿它和两个服务端数据库直接排名。

warm-up 的准确含义是：先对样本列表的前三条 path 各做一次不计时调用，再正式计时全部 200 条；数据库和 OS cache 都不清空。这不是一个数据库配置，也不代表所有 path 都完全预热。

因此，Table 4 可以公平地比较这个单树、ID-only、covering structure scan component；但它仍然不是完整 endpoint。每个 P95 也只来自一轮 200 条不同 path，没有多轮置信区间，所以目前把它作为方向性证据，不当成生产 SLA。

## 13:00--15:00 结果怎么读

Table 2 的 10M baseline 中，MongoDB subtree P50/P95 是 19.560/2,009.189 毫秒；PostgreSQL是 9.271/102.074；SQLite 是 8.331/96.971。这里最值得注意的是 MongoDB 从 P50 到 P95 拉得非常开，说明大 subtree 有明显尾延迟。两秒不是均值，也不是完整 endpoint；它混合了 path scan、逐文档 `FETCH`、结果传输和客户端 materialization。

再看严格对齐的 Table 4：MongoDB 是 1.442/11.913 毫秒，PostgreSQL是 0.398/3.479，SQLite 是 0.347/4.586。

所以可以明确说：在 matched structure ID scan 上，MongoDB 比 PostgreSQL 慢。如果 mentor 问倍数，P50 约 3.62 倍，P95 约 3.42 倍；主汇报最好直接报原始数值，不再笼统说“4 倍”。

Table 4 和后面的 10M breakdown 不能直接横向比较。前者平均返回 8,840.4 个 IDs，后者是 36,456.7 个；数据规模和单次结果规模都不同。

## 15:00--18:30 MongoDB bottleneck breakdown

Table 5 是重点。先说明读法：每一行都是一个完整 query variant，不是可以相加的执行阶段，所以不能把两个 P95 相减后，当成某个阶段的精确耗时。

一记录一节点、path index 加 `FETCH` 的 baseline 是 19.560/2,009.189 毫秒。把 leaf text 从 reference documents 中去掉、但仍然 `FETCH` 时，结果是 22.405/252.782。P95 大幅下降，说明尾部对大文档 fetch 和 materialization 很敏感；但 P50 没改善，而且两行返回字段不同，第一行还来自另一轮 run，所以不能说“拆掉 text 会让所有 latency 都下降”。

最干净的对照在同一个 Structure collection 内。Documents、query 和 ID-only 输出都不变：普通 path index 需要 `FETCH` 时是 14.552/154.826；换成 `(path, node_id)` covering index 后是 8.938/93.192。P50 和 P95 都下降约四成，explain 也确认 `docsExamined = 0`。这才是 covering index 有效的直接证据。

另一个观察是：不含正文的 reference documents 做 covered ID scan 是 9.162/101.417，单独 Structure 做 covered scan 是 8.938/93.192，两者接近。这说明本轮最主要的收益来自 projection 被索引覆盖，不能把收益简单归因于“多拆了一个 collection”。

最后一行是 all-node metadata stress case：先做 covered Structure scan，再把全部 IDs 每 1,000 个一批去 Metadata 取 title 和 summary。整个 variant 是 42.404/471.550 毫秒。

471.550 毫秒不只是“metadata index 时间”。它包括约 37 次 batch query、网络往返、cursor 处理、BSON decoding 和 Python 迭代；脚本也没有 merge 顺序或格式化树。它还是一个压力上界，因为给全部三万多个 descendants 补 metadata，未必是真实 formatter 的需求。

如果真实 endpoint 只展示有限 depth、前 K 个或当前可见节点，metadata 成本可能低很多。反过来，如果产品确实要求给三万多个节点全量返回 title 和 summary，就不能只拿 93.192 毫秒的 structure scan 声称优化完成，metadata resolution 会成为必须处理的主要成本。

## 18:30--20:00 收尾

现在能下三个结论。

第一，四个直接读操作中，subtree 才是当前的主要性能问题。

第二，在 Table 4 对齐的 structure ID scan component 上，MongoDB 确实比 PostgreSQL 慢，这个比较是公平的。

第三，MongoDB 内部已经证明的优化，是避免大 subtree 逐个 `FETCH` 包含正文的文档，并用 covering index 返回 IDs。还没证明的是：三个 collection 一定优于两个 collection，或者这些数字代表完整 `get_subtree`。

下一步应该测真实 routed endpoint，并拆成：

```text
root lookup
+ tree_id/depth-bounded structure scan
+ metadata resolution
+ selected text fetch
+ ordering and formatting
```

同时比较三种真实输出：只返回 IDs；只给可见或前 K 个节点补 metadata；给全部节点补 metadata作为压力上界。布局上再用相同输出比较一记录一节点、正文单独拆出的两集合，以及 Structure/Metadata/Text 三集合。

最后一句可以这样收：

> 这轮已经证明 MongoDB 的公平 structure scan 慢于 PostgreSQL，也证明 covering index 能明显降低 MongoDB 的 subtree 尾延迟；但三集合完整 endpoint 是否更优，取决于真实请求要给多少节点解析 metadata，这部分还需要端到端测量。

## 备用追问

### “所以 MongoDB 的 `get_subtree` 比 PostgreSQL 慢，对吗？”

准确说，是 matched structure descendant-ID scan 慢。完整的 `get_subtree(tree_id, node_id, depth)` 还没有测。

### “这个比较公平吗？”

Table 4 是公平的 component comparison：相同五个 structure 字段、相同 path、相同 ID 输出和平均结果数，而且都走 covering index。Table 2 只作为整体背景。

### “到底是不是 4 倍？”

本轮 matched 结果是 P50 约 3.62 倍、P95 约 3.42 倍。最好直接报 MongoDB 1.442/11.913 和 PostgreSQL 0.398/3.479 毫秒。

### “两秒是完整 subtree 吗？”

不是。它只计时已知 path 后拉取全部 descendant IDs；没有 root lookup、depth、metadata、排序或格式化。

### “Structure 到 Metadata 是指针吗？”

不是，是 key lookup。设计上用 `(tree_id, node_id)`；当前单树 breakdown 简化成全局唯一的 `_id = node_id`。

### “不是只 search 两次 index 吗？”

是两个索引阶段，不是两次请求。当前是一次 structure range query，加 `ceil(N/1000)` 次 metadata batch query；平均约 37 批。

### “为什么不把 metadata 放回 Structure？”

这是下一步应测的候选。如果大部分调用需要全量 metadata，两集合或部分内联可能更合适；如果只给少量可见节点补 metadata，三集合可能更有利。

### “都在内存里，为什么还有两秒？”

内存减少磁盘 I/O，但不会消除三万多个 index entries、逐文档 `FETCH`、BSON materialization、结果传输和客户端迭代。

### “warm cache 是什么配置？”

不是配置。只先执行前三条样本各一次，随后计时全部 200 条，期间不清数据库或 OS cache；没有声称所有数据都 fully warm。

### “SQLite 为什么快？”

SQLite 在 benchmark 进程内运行，没有 client-server protocol。它只提供 embedded context，主要的服务端比较是 MongoDB 对 PostgreSQL。
