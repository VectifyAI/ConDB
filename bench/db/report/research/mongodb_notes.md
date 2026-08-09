# MongoDB 输赢分析笔记(讨论准备用,不随报告外发)

基于 bench/db 实测数据 + MongoDB 官方文档核实。DuckDB 不在讨论范围(OLTP 点读/写全面不适用)。

---

## 一、MongoDB 基础(这次涉及的机制)

1. **文档模型**:存取单位是整个 BSON 文档。一个节点的所有字段(title / summary / text / path)物理上存在一起。
2. **WiredTiger 引擎**:B-tree 存储;MVCC,但旧版本文档**自动回收**(没有 PG 那种 VACUUM 债);盘上块级 snappy 压缩,**缓存中的页是解压过的**(所以热查询慢与解压无关)。
3. **二级索引是独立 B-tree**,只存「索引字段值 + record id」。索引命中后,要返回任何非索引字段,必须回表 **fetch 整个文档**再做投影——这是理解一切输赢的关键机制。
4. **Covered query(覆盖查询)**:谓词字段和返回字段**全部**落在**同一个**复合索引里(索引交集不行),且投影显式 `_id: 0`,才能不回表,`explain` 显示 `PROJECTION_COVERED`、`totalDocsExamined: 0`。1024 字节索引键上限 4.2 版已移除,大 summary 做索引键合法——但是否真的快,无人测过(胖键破坏「索引键小、常驻缓存」这个 covered query 快的前提)。
5. **16 MiB 文档硬上限**(官方明确不会调高,wire protocol 48MB 约束);BSON 嵌套 100 层上限;超大负载走 GridFS。
6. **官方 schema 模式**(讨论时用官方词汇):
   - *Subset Pattern* —— 冷热字段拆两个集合(= 我们的「拆 text」方案,官方背书);
   - *Extended Reference* —— 只内嵌高频访问字段;
   - *Materialized Paths* —— path 字符串 + 锚定前缀正则 `find({path: /^,A,B,/})`,内部转成索引范围扫描,与我们的字节范围谓词执行等价;
   - *Nested Sets* —— left/right 区间编号,静态树取子树的官方最优解,改树要重编号。
7. **`$lookup`(跨集合 join)被官方列为反模式**("slow and resource-intensive...consider restructuring your schema")。6.0 的 SBE 执行引擎改进过,但手册里的定性指导没撤。

## 二、MongoDB 为什么输

### 1. get_subtree 尾延迟(唯一数量级落败:P95 2478ms vs PG 123ms / SQLite 135ms)

机制链,一步步:

- path 索引只给 record id → 每个命中节点都要**完整 fetch + 物化整个 BSON 文档**(包括没人要的大 text 字段)→ 投影时再丢掉 → 3.6 万条结果走游标分批传输;
- 代价 ∝ 文档数 × 文档大小,「读的单位是文档」没法绕过;
- 数据全在缓存,与磁盘 I/O、解压无关——纯粹是逐文档的 CPU 工作;
- **超线性证据**:P95/P50 = 80×(PG/SQLite ≈ 11×,和子树大小分布一致),说明最大子树上单文档成本本身也在涨,雪上加霜。

### 2. 点读/子节点/取内容比 PG 慢约 3×(0.27ms vs 0.09ms)

每调用的客户端-服务器开销:协议处理、BSON 序列化、驱动路径。亚毫秒级,无实际后果,但排名是落后的——报告里已主动说破。

### 3. 写路径比 PG 慢约 2×(更新 0.242ms vs 0.123ms;吞吐 4,050 vs 7,856 ops/s)

单条更新的往返 + 文档级处理开销。换来的是零膨胀(见下)。

### 4. 单调用对比被 SQLite 全面碾压

进程内函数调用 vs 网络往返,是**架构差异不是引擎差异**——这条在讨论里要主动框定,否则比较没意义。

### 5. 没测但已知的潜在弱点(对方可能主动问)

- `delete_tree`:WiredTiger 删除后**不自动归还磁盘空间**,要手动 `compact`;
- 跨集合 `$lookup`:拆 text 方案会引入,官方自己的反模式,代价未测;
- 工作集超 RAM 后的退化:全内存环境没暴露。

## 三、为什么 PostgreSQL 赢(它赢的项)

1. **子树查询**:行存只物化请求的列。`node_id` 是独立列,宽 text 留在行里**根本不读**,JSONB datum 甚至不用解压。逐行成本 << 逐文档 fetch。
2. **点读延迟**:线协议更轻,低并发下每调用路径短。
3. **它输的**(MongoDB 的对应赢面):
   - 磁盘 17.71GB,约 3 倍于 Mongo(5.81GB,snappy);
   - 每次 `jsonb_set` 整行写新版本 → 死元组 → VACUUM 债(实测 7MB 膨胀;Mongo 为 0);
   - 并发 16 客户端左右见顶(每连接一进程模型),Mongo 吞吐全程随客户端上升。

## 四、为什么 SQLite 赢(它赢的项)

1. **嵌入式**:查询就是进程内函数调用——零网络、零协议、零序列化。点读 0.008–0.012ms,比谁都快一个量级以上。
2. **写吞吐最高**(40k updates/s, WAL 模式):同样因为没有客户端往返。
3. 更新零膨胀(与 Mongo 同)。
4. **它输的**:
   - 并发:单写者 + 多进程争一个文件,几个客户端后吞吐开始退化(并发图里最早见顶、最早衰减);
   - 磁盘最大(19.51GB);
   - 没有服务化/远程访问/水平扩展可言。
5. **关键定位**:SQLite 是 ConDB 现状。这份数据本身**不构成"为性能而迁移"的理由**——单机单进程下它全面最快。迁移到服务器型数据库的理由只能是并发、多进程/多机访问、服务化。

## 五、讨论时的一句话弹药

- 「MongoDB 每项都比 PostgreSQL 慢约 3 倍,但只有 get_subtree 慢出了数量级。」
- 「输的机制 = 赢的机制,都是 document-at-a-time:整文档 fetch 让大子树读贵,也让点读快(实体聚合)、原地更新便宜、布局可分片、盘上紧凑。」
- 「修复全部在社区版配置层,不动引擎:覆盖索引(胖键效果待实验)、拆 text(官方 Subset Pattern,直接引文档)、嵌套分桶(16MiB 算术:~1.5kB/节点 × 36k ≈ 55MB,必须分桶)、保留 materialized path。」
- 「16MiB、覆盖查询条件、$lookup 反模式、Nested Sets 适用静态树——全部出自 MongoDB 自己的手册,可逐条给链接。」

## 六、引用清单

- MongoDB limits(16MiB、嵌套 100 层、索引键上限移除):mongodb.com/docs/v7.0/reference/limits/
- Covered query 条件与 `_id:0`:mongodb.com/docs/manual/core/query-optimization/
- Subset Pattern:mongodb.com/docs/manual/data-modeling/design-patterns/group-data/subset-pattern/
- $lookup 反模式:mongodb.com/docs/manual/data-modeling/design-antipatterns/reduce-lookup-operations/
- Materialized Paths / Nested Sets:mongodb.com/docs/manual/applications/data-models-tree-structures/
- 冷热运行分开报告(方法论):Raasveldt et al., *Fair Benchmarking Considered Difficult*, DBTest'18
