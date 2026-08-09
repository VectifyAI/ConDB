# MongoDB JSON 树场景:优化手段 × 挑战矩阵(讨论准备用,不随报告外发)

来源:deep research(105 agent,3 票对抗验证,25 条断言全过)+ MongoDB 官方文档核实。
目标负载:write-once 的 10M 节点文档树,Community Edition,痛点是 `get_subtree` 尾延迟 P95 2.5s。

---

## 0. 头号洞察:get_subtree 根本不需要 text 字段

报告自己的定义:`get_subtree` 是"渲染树视图 / 拉一个 block",返回 **node_id + title + summary**;节点正文(text)是后续 `get_entity` 对 LLM 选中的少数节点单独取的。

→ 也就是说:**把大 text 拆出去(Subset Pattern),对 get_subtree 这条热路径没有任何代价**——通用文档里说的"拆出去后取 text 要多一次往返",那次往返发生在 `get_entity`,而它本来就是独立操作、本来就只取少数节点。

这把根因直接消掉:get_subtree 慢是因为扫 3.6 万个节点时每个都要物化整份 BSON(含没人要的 text);text 一旦不在主集合里,扫描物化的就只剩小文档。**这是所有手段里杠杆最高、代价最低的一条,且对本负载几乎零副作用。**

根因机制(已核实):WiredTiger 缓存里的集合数据是**未压缩**的;path 索引只给 record id,命中后必须 fetch+解压整份文档再投影;3.6 万份大文档塞满缓存触发驱逐(95% 触发、80% 目标),拖慢所有线程。所以瓶颈是"物化了什么",不是"用什么查询算子"。

---

## 1. 技术 × 挑战矩阵(按杠杆排序)

### 第一档:直击根因(改"物化了什么")

| 手段 | 机制 | 挑战 / 失效场景 | 治尾延迟? | 建议 |
|---|---|---|---|---|
| **Subset Pattern**(拆 text 到独立集合) | 热文档只剩 structure+summary,工作集变小、装得进缓存,范围扫描只物化小文档 | 通用代价是"取 text 要多一跳"——**但本负载 get_subtree 不取 text**,这一跳落在 get_entity(本就独立),故几乎无副作用 | **是,直接** | **ADOPT** |
| **Nested Sets**(left/right 区间) | 入树时 DFS 编号,取子树 = 一条索引区间查询 `find({left:{$gt},right:{$lt}})`,完全不递归 | 插入/删除要 O(n) 重编号——但**write-once,重编号只在 ingest 付一次**;单字段更新不触发重编号 | **是,把 36k 次 fetch 变一条区间查询** | **ADOPT(write-once 正中适用条件,官方原话 "best for static trees")** |
| **预计算物化子树**(ingest 时把每个子树根的树视图存成一份文档) | write-once → 直接把渲染好的 structure+summary 视图按子树根落盘,get_subtree 退化成一次点读 | 重叠子树带来存储放大;16MiB 上限对大子树要分块;只对"有界的子树根"划算(恰好是 benchmark 采样的 depth≤3 根) | **是,最彻底** | **EXPERIMENT(write-once 最契合,但放大系数未量化)** |

### 第二档:结构性、有条件

| 手段 | 机制 | 挑战 / 失效场景 | 治尾延迟? | 建议 |
|---|---|---|---|---|
| **Clustered collection**(5.3+,_id=树序) | 文档按 _id 物理有序、与索引同文件,一次读而非两次,子树扫描变顺序读 | **只能 clustered 在 `{_id:1}`**——必须把 DFS/path 序编进 _id 本身才有用;有可用二级索引时优化器不自动选,要 hint | 是,前提是 _id=树序 | **EXPERIMENT** |
| **Bucket Pattern**(把多节点折进嵌套桶文档) | N 个小文档塞进少数桶文档,文档数和索引项数都降 | 16MiB 上限(1.5kB/节点 → 单桶上限约万级,实际更少);任何单字段更新 = 整桶重写 | 部分(减少文档数和索引 RAM) | **EXPERIMENT(与预计算子树重叠,二选一)** |

### 第三档:支撑 / 部分缓解(单独都不够)

| 手段 | 机制 | 挑战 / 失效场景 | 治尾延迟? | 建议 |
|---|---|---|---|---|
| **WiredTiger 缓存调大** | 缓存设到能装下工作集 | 若工作集(含 text)结构上就 > RAM,调缓存无用——所以必须先 Subset Pattern | 仅作支撑 | ADOPT 作辅助,别单靠 |
| **zstd 块压缩 + path 索引前缀压缩** | zstd 省盘;前缀压缩对 materialized-path 这种长公共前缀去重、省索引 RAM | 缓存里数据是**解压**的,压缩省盘/IO 不省扫描 CPU;前缀压缩救不了多 KB 的 summary 覆盖键 | 间接 | EXPERIMENT(顺手做,非解药) |
| **锚定前缀正则 `/^,Books,/`** | 走标准升序索引、构造区间扫描(这是现状访问路径) | 非锚定正则 `/,X,/` 无法构造区间,必须扫全索引;必须大小写敏感 | 现状已对;尾延迟**不是它的错** | KEEP(不要改成非锚定) |

### 第四档:不是答案 / 回避

| 手段 | 机制 | 挑战 / 失效场景 | 治尾延迟? | 建议 |
|---|---|---|---|---|
| **覆盖索引** `{path,node_id,title,summary}` + `_id:0` | 想 index-only、不回表 | 多 KB summary 当索引键 → 索引膨胀到接近集合本身、逐键扫描 CPU 上升;前缀压缩救不了;**是否真比 fetch 快无人测过**(open question #1) | 理论上是,实际存疑 | EXPERIMENT-但悲观(不如 Subset 简单可靠) |
| **$graphLookup** | 递归 parent_id 遍历 | 7.0 里 100MB 上限且**忽略 allowDiskUse**(8.2 才修);仍逐文档物化;打不过索引区间扫描 | 否 | **AVOID 作主路径** |
| **列式旁路存储** | 给全语料分析用 | 检索路径根本不做全语料扫描 | 不相关 | 本热路径**不需要** |

---

## 2. 推荐组合(write-once / Community)

**Subset Pattern(拆 text)+ Nested Sets 或 树序 _id(配 clustered collection 让扫描顺序化)。**

逻辑:真正的杠杆是"让被扫描的工作集里没有大 text",不是换查询算子。write-once 让 Nested Sets 的重编号、Bucket 的整桶重写都只在 ingest 付一次,主要缺点被中和。normalize(利于增量插入)vs denormalize(利于子树读)的张力,在"写极少"的前提下应倒向 denormalize。

落地顺序建议:
1. 先做 **Subset Pattern**——最简单、机制最稳、对本负载零副作用,单这一步可能就够;
2. 不够再加 **Nested Sets / 树序 _id**,把区间查询也优化掉;
3. 仍要极致再上 **预计算物化子树**(get_subtree → 点读)。

---

## 3. 元挑战(讨论时绕不开)

1. **根因是 document-at-a-time 读模型**:几乎所有修法都是"绕开它"——把大字段移出扫描集、或把多次 fetch 折成一次区间/点读——而不是消除它。关系型引擎天然只投影一列、永不碰 text blob,这正是它 120ms 的原因。
2. **Community 约束**:没有 Atlas Search、列式、online archive,所以上面全部限定在 schema/index/存储引擎配置层。
3. **快子树读 vs 廉价增量写的张力**:denormalize 利前者、normalize 利后者;本负载写极少 → 倒向 denormalize。
4. **16MiB 天花板**:逼着 Bucket / 预计算子树都得分块(1.5kB/节点 × 36k ≈ 55MB,远超 16MiB)。

---

## 4. "MongoDB 是不是错的工具" —— 正反两面

**支持"是错的工具":**
- 这个操作本质是"扫很多行、只取一个字段"的投影密集型范围扫描;关系型/列式只读那一列、永不物化 text,实测快 20 倍(120ms vs 2.5s,用户自测)。
- 文档模型"物化整文档"与"扫多行读一字段"结构上错配——这是设计取向冲突,不是配置问题。

**反对(MongoDB 没问题):**
- **其他每一项** Mongo 都占优或够用:点/子节点/内容读亚毫秒、更新零膨胀、并发吞吐持续上升、盘上紧凑。
- 唯一短板可修到可接受(未必到 120ms,但有望 < 1s 甚至更低),且因 write-once,代价只付一次。
- **关键**:get_subtree 视图不需要 text,Subset Pattern 把根因干净地移走——所以"错配"只在"坚持把 text 内联且不能 denormalize"时才成立。

**我的裁决:** 对**这个**负载(write-once、子树视图只要 structure+summary 不要 text),只要做 Subset Pattern,MongoDB **不是**错的工具——因为那让被扫描的工作集变得很小,document-at-a-time 的惩罚随之坍缩。"错的工具"结论只在你坚持内联 text 且拒绝 denormalize 时才成立。
**诚实保留:** 没有任何人实测过 Subset+X 真能把 2.5s 拉到 120ms 量级——机制成立但**未量化**。而这个决定性实验很便宜(bench/db 框架已在)。

---

## 5. 未决问题(只能靠实验答,不是再查资料)

1. 覆盖索引在多 KB summary 键下到底能不能 index-only、还是被索引膨胀和扫描 CPU 吃掉收益?
2. Subset + Nested Sets(或 树序 _id + clustered)在 10M 树上,36k 子树 P95 究竟能从 2.5s 拉到多少?能否逼近 120ms?
3. 预计算物化子树在 10M 节点、子树重叠下的存储放大系数多少?16MiB 在哪里逼着分块?

→ **下一步最值钱的动作:在 bench/db 里实测 Subset Pattern 的 before/after(以及叠加 Nested Sets)。** 这一步直接回答上面全部三问,也补上整份分析唯一的缺口(无量化基准)。

---

## 6. 重要时效性 & 证据缺口

- **$graphLookup 的 100MB/忽略 allowDiskUse 是 7.0 的事,8.2(SERVER-23980)已修可落盘**——若升级过 8.2 别再沿用此限制。
- **证据性质**:几乎全是 MongoDB 官方一手文档(机制/约束权威),但**无任何独立量化基准**说"Subset 把 P95 从 2.5s 降到 X"。所有收益是定性的("显著改善""缩小工作集"),"何时无效/真实失效模式"列更多靠机制推理而非实测对照。
- Nested Sets 的"一条区间查询取子树"那条是唯一非全票(2-1),分歧在"index-backed"措辞(该文档页本身不建索引),核心机制无争议。

## 7. 引用
- Subset Pattern:mongodb.com/company/blog/building-with-patterns-the-subset-pattern
- Nested Sets:mongodb.com/docs/v7.0/tutorial/model-tree-structures-with-nested-sets/
- Bucket Pattern:mongodb.com/company/blog/building-with-patterns-the-bucket-pattern
- Clustered collections:mongodb.com/docs/v7.0/core/clustered-collections/
- Materialized Paths:mongodb.com/docs/manual/tutorial/model-tree-structures-with-materialized-paths/
- WiredTiger(缓存未压缩/驱逐/压缩):mongodb.com/docs/manual/core/wiredtiger/ ; source.wiredtiger.com/mongodb-5.0/tune_cache.html
- $graphLookup 限制:mongodb.com/docs/v7.0/reference/operator/aggregation/graphlookup/
