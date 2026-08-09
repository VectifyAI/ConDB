# 为什么 MongoDB 查一大棵子树会慢？

这是一版可以直接照着讲的 20 分钟口语稿。正文只围绕 subtree，术语第一次出现时都用大白话解释。最后附一页提词卡和追问答案。

## 0:00--2:00 先把问题说简单

老师，今天我不打算逐张表念数字，我只讲一个问题：为什么 PageIndex 查一个节点下面的整棵子树时，MongoDB 会突然慢下来。

先说，这不是 MongoDB 连一个节点都查得慢。在一千万节点的数据上，查一个节点、列它的直接 children、或者读取一个已经选中的节点正文，MongoDB 的 P95 都不到 0.35 毫秒。这几个操作现在都不是主要问题。

问题出在 subtree。因为 subtree 不是取一条数据，而是从一个位置往下取很多后代。这轮大的测试里，一次平均要拿回 36,456.7 个 node IDs。结果一多，数据库到底只是顺着索引读，还是还要把三万多个原文档逐个翻出来，差别就会非常大。

这轮最核心的发现其实很简单：

> MongoDB 原来的查询虽然用了 path index，但这个 index 里的信息不够，所以每匹配一个节点，还要回到原文档读一次。把 node ID 也放进 index，让查询只看 index 就能返回结果以后，尾延迟下降了大约四成。

不过，拿到三万多个 IDs 以后，如果我们又给每个 ID 都去查 title 和 summary，那后半段还是会慢。所以现在只能说第一段优化有效，不能说整个三集合方案已经优化完成。

## 2:00--5:00 一次真正的 subtree 请求要做什么

先用一个具体例子讲。

假设 PageIndex 是一本书的目录树，我现在站在“第二章”这个节点，想看它下面三层。真正的 `get_subtree(tree_id, node_id, depth)` 大概需要五步。

第一步，用 `tree_id` 和 `node_id` 找到“第二章”在树里的位置，也就是它的 path 和 depth。

第二步，根据这个 path 找到下面的所有后代。

第三步，用 depth 把太深的节点去掉，并把 root 自己放进结果。

第四步，拿到这些节点的 title 和 summary，按树的顺序排好。

第五步，formatter 根据 parent-child 关系把结果重新组织成一棵能展示的树。正文一般不用给所有节点都取，只在最后选中某个节点时再读。

这次 benchmark 只测了第二步，而且它连第一步都已经替我们做好了：测试脚本事先知道 path，然后直接按 path 扫下面所有后代。

比如“第二章”的 path 是 `/book/chapter2`，那它所有后代都会以 `/book/chapter2/` 开头。数据库里的字符串是排好序的，所以脚本用一个起点和终点，把这一整段圈出来：

```text
path >= P + "/"  and  path < P + "0"
```

然后只拿这些记录的 node ID。

所以报告里虽然把这一行叫作 `get_subtree`，更准确的说法其实是：

> 已经知道 path 以后，把这个 path 下所有后代的 IDs 扫出来。

它不包括找 root，不包括 depth 限制，不包括 root 自己，不排序，不分页，也不拿 title、summary、正文，更没有格式化整棵树。

还有一个边界：这轮每个 dataset 只有一棵树，所以实测 query 里没有 `tree_id`。Figure 1 画的 `{tree_id, path, node_id}` 是将来多树部署时的设计；本轮真正测的 index 是 `(path, node_id)`。我会把“设计”与“这次实测”分开讲。

## 5:00--8:00 MongoDB 为什么会慢：明明有索引，为什么还要读原文档

这里可以把 index 想成一本书后面的索引页。

如果索引页只写了关键词和页码，我查到关键词以后，还得按照页码把正文翻开，才能看到我要的信息。MongoDB 这里也是一样。

原来每个节点是一份完整 document，里面同时放着：

- 它在树里的位置；
- title 和 summary；
- 页码范围；
- 可能很长的 leaf text。

普通 path index 主要告诉 MongoDB：哪些原文档的 path 在这个范围内，以及这些原文档放在哪里。但是查询还要返回 node ID，而原来的 path index 并没有完整覆盖返回内容，所以 MongoDB 的执行过程是：

```text
先扫 path index
→ 找到一个匹配项
→ 回到原 BSON document 读一次
→ 取出需要的字段
→ 对下一个匹配项重复
```

MongoDB explain 里把“回到原文档读一次”叫 `FETCH`。

如果只命中一个节点，`FETCH` 一次问题不大。但一次 subtree 平均命中三万六千多个节点，就会重复很多次。文档里还可能带着很大的正文。正文并没有传回客户端，但数据库内部仍要定位并处理这些大文档。

有人可能会问：整份数据不是都能放进内存吗，为什么还能慢到秒级？因为“在内存里”只代表少了磁盘读取，不代表工作消失。数据库还是要找三万多个 document、处理 BSON、把结果交给 driver、经过本机 client-server 通信，再由 Python 把所有结果放进 list。数据量一大，这些步骤照样会形成长尾。

这也解释了为什么点查很快、subtree 却慢：问题不是有没有 index 这么简单，而是一个 index match 后面还跟着多少次回表，以及最后要搬多少结果。

## 8:00--10:30 三集合到底在做什么

三集合可以想成把一个很厚的档案袋拆成三个柜子。

第一个柜子叫 Structure，只放目录骨架：tree ID、node ID、parent ID、depth 和 path。

第二个柜子叫 Metadata，放 title 和 summary。

第三个柜子叫 Text，放体积最大的正文。

查 subtree 时，第一步只需要目录骨架和 node IDs，所以先扫 Structure。我们把 path 和 node ID 一起放进 index。这样 index 本身已经有全部返回内容，MongoDB 不用再打开 Structure document。这个就叫 covering index；大白话就是“索引里的东西已经够用了，不需要回原文档”。

Structure 到 Metadata 也不是一个指针，更不是 MongoDB `DBRef`。它就是拿 node ID 再做一次 key lookup。生产设计应该用 `(tree_id, node_id)`；这轮单树实验为了简单，确认 node ID 全局唯一以后，直接用 `_id = node_id`。

这里还有一个很容易说错的地方：逻辑上是两个索引阶段，不代表只发两次请求。

第一阶段是一次 Structure range query，得到三万多个 IDs。第二阶段的脚本每 1,000 个 IDs 打一个包，到 Metadata 查 title 和 summary。平均 36,456.7 个 IDs，就大约是 37 个 metadata requests。

正文不应该跟着整棵 subtree 一起读。应该等检索过程最终选中了少量节点，再按 node ID 到 Text 点查正文。

所以三集合想解决的是：查目录时不要搬正文，并且让第一段扫描只看 index。但是它也带来一次从 Structure 到 Metadata 的二次查询。如果每个 subtree 都要给几万个节点补 metadata，这个二次查询可能把省下来的时间又吃掉一部分。

## 10:30--13:30 公平性到底怎么保证

报告里有两种实验，不要混在一起。

Table 2 是先看整体情况。三个数据库都用一条记录保存一个完整节点，也用同一棵树和同一组 path。它告诉我们哪一类操作值得继续研究。但三个 driver 最后投影的字段还有小差异，MongoDB 默认还会带 `_id`，所以 Table 2 适合看现象，不适合拿来算一个非常精确的“MongoDB 慢几倍”。

真正回答公平不公平的是 Table 4。

我给 MongoDB、PostgreSQL 和 SQLite 都单独建了一个 Structure store，而且都只有完全一样的五个字段：tree ID、node ID、parent ID、depth 和 path。

然后三边使用完全相同的 200 条 path，平均每条都返回 8,840.4 个 IDs，最后也都只返回 node ID。

最关键的是，三边都确认没有回原数据：MongoDB 是 covered index scan，PostgreSQL 是 index-only scan，SQLite 是 covering-index scan。名字不同，意思一样——需要的 node ID 直接从 index 里拿。

MongoDB 和 PostgreSQL 都是同一台机器上的 Docker 服务，所以它们是主要的 server-to-server 对比。SQLite 是直接跑在 Python 进程里的，没有 client-server 通信，只能当一个嵌入式数据库的参考，不能拿它直接证明谁的服务端更快。

每个 variant 开始时，脚本先把样本列表里的前三条 path 各跑一次，不计时，然后正式计时全部 200 条。期间不会主动清数据库或操作系统 cache。这不是数据库里的某个“热缓存开关”，也不代表 200 条 path 全都提前跑过。

还有一点：这里的 P95 是 200 条不同 path 各测一次以后排出来的，不是同一条 query 重复 200 次。不同 path 的 subtree 大小本来就不同，所以这个 P95 同时反映结果规模差异和运行时波动。它能说明尾部问题，但现在还不是带置信区间的生产 SLA。

在这些限定下，Table 4 的比较是公平的。但公平的是“已知 path 后，只返回 IDs 的 Structure scan”，不是完整的 `get_subtree` endpoint。

## 13:30--16:00 三组最重要的数字

我觉得现场只要记住三组数字，不需要把所有表都念一遍。

第一组是原来一记录一节点的 10M baseline。

MongoDB 的 subtree P50 是 19.560 毫秒，P95 是 2,009.189 毫秒。PostgreSQL是 9.271 和 102.074 毫秒。

P50 可以理解成一半请求比它快、一半比它慢。P95 可以理解成把 200 条结果从快到慢排好，大约排到第 190 条的位置。MongoDB 从 19.6 毫秒跳到两秒，说明它的尾部非常长。

不过这组数字混合了大文档 `FETCH` 等因素，所以它主要告诉我们“这里有问题”，不拿它做最严格的引擎倍数结论。

第二组是 Table 4 的公平 Structure scan。

- MongoDB：P50/P95 是 1.442/11.913 毫秒；
- PostgreSQL：0.398/3.479 毫秒；
- SQLite：0.347/4.586 毫秒。

所以，在同字段、同 path、同返回 IDs、都不回表的情况下，MongoDB 仍然比 PostgreSQL 慢。要讲倍数的话，P50 大约 3.62 倍，P95 大约 3.42 倍。这里最好直接报原始数值，不再笼统说“4 倍”。

第三组是 MongoDB 自己内部最干净的前后对比。

同一个 Structure collection、同样只返回 IDs：普通 path index 需要 `FETCH` 时是 14.552/154.826 毫秒；换成 covering index 后是 8.938/93.192 毫秒。P50 和 P95 都降低大约四成。

这组才真正证明：covering index 有用。

## 16:00--18:30 breakdown 还告诉了我们什么

Table 5 每一行都是一种完整查询方法，不是先做第一行、再做第二行的流水线，所以不能把两行的 P95 直接相减，说差值就是某一步的时间。

先看大文档。原始完整节点 document 的 P95 是 2,009.189 毫秒。构造一个没有 leaf text 的 reference document、仍然需要 `FETCH`，P95 降到 252.782 毫秒。这个现象说明大文档 fetch 很可能是长尾的重要来源。

但这不是纯对照：两行返回字段不同，P50 也从 19.560 变成 22.405，并没有更快。所以准确说法是“大文档与尾延迟有关”，不能说“拆掉 text 后每个请求都会变快”。

再看物理拆 collection 本身有没有额外魔法。不含正文的 reference documents 做 covered ID scan，P50/P95 是 9.162/101.417；单独的 Structure collection 做 covered scan，是 8.938/93.192。两个结果很接近。

这说明目前最确定的收益来自 covering index，而不是 collection 数量本身。把数据拆成三个集合是为了让 covering 更容易、让正文不参与扫描；但不能把“拆”本身说成性能来源。

最后看 Metadata。Covered Structure scan 后，给全部 IDs 每 1,000 个一批补 title 和 summary，整个过程的 P50/P95 是 42.404/471.550 毫秒。

这里的 471.550 毫秒不只是 metadata index 的时间。它还包括大约 37 次请求、client-server 往返、cursor 处理、BSON 解码和 Python 遍历。脚本甚至还没有按照 Structure 的顺序把两边 merge，也没有格式化树。

如果拿同一轮里不含正文、metadata 直接放在 document 内的方案看，它的 P95 是 252.782 毫秒；全量 metadata 的三集合压力路径是 471.550 毫秒。因此当调用方真的需要全部节点的 metadata 时，这次结果并没有证明三集合更快。它恰恰提醒我们，拆开以后会付出 fan-out 查询的代价。

但这也不是说三集合一定更慢。真实 `get_subtree(depth)` 可能只要两三层，formatter 也可能只显示前 K 个或者当前可见节点。要是只给几十个节点补 metadata，37 批请求就不会出现。现在最大的未知数，就是产品实际需要多少 metadata。

## 18:30--20:00 最后怎么总结

最后我会把结论收成三句话。

第一，MongoDB 不是所有读取都慢；问题集中在一次返回很多结果的 subtree scan。

第二，在 Table 4 对齐的 Structure ID scan 上，MongoDB 的确比 PostgreSQL 慢，这个 component comparison 是公平的。MongoDB 内部用 covering index 避免逐文档 `FETCH`，P50 和 P95 都改善了大约四成。

第三，三集合解决了“扫描目录时不要碰正文”的问题，但它把 metadata 变成第二阶段。到底是拆成两个集合还是三个集合，取决于一次真实请求需要给多少节点补 metadata，现在还不能下最终结论。

下一步应该测完整 endpoint，而不是继续只测一段。完整计时至少要包括：

```text
用 tree_id + node_id 找 root
→ 按 depth 扫 Structure
→ 给真正需要的节点补 Metadata
→ 排序并格式化树
→ 对最终选中的少量节点读 Text
```

然后用完全相同的输出，比较一记录一节点、正文单独拆出的两集合、以及三个集合。Metadata 还要分三种场景：只返回 IDs；只补前 K 个或可见节点；全部节点都补，作为压力上界。

最后一句可以直接这样讲：

> 现在我们已经知道 MongoDB 慢在大结果的 Structure scan，也知道 covering index 能解决其中一部分；接下来真正要决定三集合值不值得，关键不是再盯着 index，而是先弄清楚一次真实 subtree 到底要给多少节点取 metadata。

## 一页提词卡

### 只记这四句话

1. 本轮测的是“已知 path 后扫描全部 descendant IDs”，不是完整 `get_subtree(tree_id, node_id, depth)`。
2. Table 4 才是严格公平对比：MongoDB 1.442/11.913 ms，PostgreSQL 0.398/3.479 ms。
3. MongoDB Structure `FETCH → covering`：14.552/154.826 ms 降到 8.938/93.192 ms，约改善四成。
4. 全节点 metadata 是一次 Structure query 加约 37 批查询，组合路径 42.404/471.550 ms；三集合端到端还没证明。

### Mentor 可能追问

**“所以 MongoDB 的 `get_subtree` 比 PostgreSQL 慢？”**

准确说，是公平条件下的 Structure descendant-ID scan 慢；完整 endpoint 还没测。

**“到底是不是 4 倍？”**

不是统一的 4 倍。本轮 matched 结果是 P50 约 3.62 倍、P95 约 3.42 倍，最好直接报原始数值。

**“Structure 到 Metadata 是指针吗？”**

不是，就是按 key 再查一次。生产设计用 `(tree_id, node_id)`；当前单树实验简化成 `_id = node_id`。

**“不是查两次 index 就行了吗？”**

是两个索引阶段，但不是两次请求。当前平均是一次 Structure query 加约 37 个 Metadata batch queries。

**“为什么不直接把 title 和 summary 放在 Structure？”**

可以，这正是下一步应该与三集合做同输出比较的候选。如果大多数请求需要全量 metadata，放回去可能更合适。

**“为什么数据在内存里还有两秒？”**

内存只省磁盘 I/O，不省三万多个 `FETCH`、BSON 处理、结果传输和客户端遍历。

**“warm cache 是数据库配置吗？”**

不是。脚本只先跑前三条样本各一次，然后计时全部 200 条，期间不清 cache。

**“SQLite 为什么快？”**

SQLite 在进程内，没有 client-server 通信，只作为嵌入式参考。主要的服务端对比是 MongoDB 和 PostgreSQL。
