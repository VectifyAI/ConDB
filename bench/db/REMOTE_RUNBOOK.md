# ConDB × MongoDB 基准:异地机器从零跑通手册

给一台全新的干净机器用。目标:在**无其他负载**的机器上把整套基准干净重跑一遍,
产出一套单机自洽、可复现的数字,替换报告中来自受争用环境的旧数。跑完只需把
`runs/*.json` 传回来。

## 0. 前因后果(为什么要重跑)

- MongoDB 主动联系我们(AI infra 场景:agent memory 以大型 JSON 树做检索),想看
  这类负载上 MongoDB 与竞品(PostgreSQL/JSONB、DuckDB、SQLite)的差距和优化方式。
  产出是两份报告(`report/report.tex` 主报告 + `report/optimization.tex` 研究报告),
  会交给 MongoDB 的人看。
- 历史教训:最早一轮多引擎同机混跑,MongoDB 的 `get_subtree` P95 被系统级争用放大到
  2.5 s(隔离重测只有 ~0.3–0.4 s),这个假数字曾进过报告。**本次重跑的第一原则:
  测量窗口内机器上不允许有任何其他负载。**
- 部署 schema(结构/元数据/正文三集合解耦,子树扫描由 `{path, node_id}` 覆盖索引
  仅凭索引服务)已在原机器上测得核心结果:10M 树上 covered scan P50 15.5 / P95 168 ms,
  约为 PostgreSQL 的 1.4 倍。绝对数值随 CPU 不同会整体偏移,**倍数关系应大致保持**,
  可作 sanity check。
- 本次要补齐/替换的:四引擎大数据集读对比(逐引擎隔离跑)、kv 解耦全部 8 档
  (原机器上最后 3 档被中断:clustered `$in`、`$lookup` ×2)、medium 在部署 schema
  下的重测,以及(可选)写入/并发/多租户,使所有表出自同一台机器。

## 1. 机器要求

- **16 vCPU / 64 GB 内存 / ≥200 GB NVMe**,固定性能型(不要 burstable / 超卖共享核)。
  内存账:客户端把 14 GB JSON 展开峰值 ~23 GB(实测外推),ingest 窗口与 mongod
  缓存(~17 GB)叠加峰值 ~40 GB;`bench_subset_kv.py` 在 ingest 后会释放 rows。
- 测量期间不跑任何别的东西;建议关掉不必要的守护进程。
- 记录机器规格,报告方法一节要更新:`lscpu | head -20; free -h; uname -r`。

## 2. 从零搭环境

```bash
# Docker(数据库都跑容器里)
curl -fsSL https://get.docker.com | sh

# 两个数据库容器(端口与脚本默认一致,勿改;绑 127.0.0.1,绝不暴露公网)
docker run -d --name condb_pg    -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=bench -p 127.0.0.1:55432:5432 postgres:16
docker run -d --name condb_mongo -p 127.0.0.1:57017:27017 mongo:7
# 64 GB 机器可不设 WT 缓存(默认取内存一半 ≈ 31 GB,够);32 GB 机器才需要手调。

# Python 环境(uv;也可用系统 pip 装同样四个包)
curl -LsSf https://astral.sh/uv/install.sh | sh
cd <解包目录>          # 包里就是 bench/db 的内容,见第 6 节
uv venv .venv --python 3.10
uv pip install --python .venv "pymongo>=4.6" "psycopg[binary]>=3.1" "duckdb>=0.10" "pyarrow>=15"
PY=.venv/bin/python
```

## 3. 生成数据(不用传 14 GB)

生成器种子确定(默认 `--seed 42`),在哪台机器生成都一样;基准脚本内部采样
(200 条子树路径等)另有固定种子 7,全套自洽。

```bash
$PY gen_pageindex.py --scale medium --out data/medium.json   # ~85 MB,秒级
$PY gen_pageindex.py --scale large  --out data/large.json    # ~14 GB,分钟级,峰值内存 ~20 GB
```

## 4. 跑批(严格顺序执行,一次只跑一个)

```bash
mkdir -p runs

# 4.1 四引擎大数据集读对比 —— 每个引擎单独一次调用,彼此隔离
#    100 GB 盘就够的关键:每个引擎测完立刻清掉它的数据(数字都在 runs/*.json 里)
for e in mongo postgres duckdb sqlite; do
  $PY bench_databases.py --doc data/large.json --engines $e \
      --out runs/read_large_$e.json 2>&1 | tee runs/read_large_$e.log
  case $e in
    sqlite) rm -f runs/_sqlite.db* ;;
    duckdb) rm -f runs/_duck*.db ;;
    mongo)  docker exec condb_mongo mongosh --quiet --eval 'db.getSiblingDB("bench").dropDatabase()' ;;
    postgres) docker exec condb_pg psql -U postgres -d bench -c 'DROP TABLE IF EXISTS nodes CASCADE;' ;;
  esac
done

# 4.2 medium 读对比(便宜,同样逐引擎)
for e in mongo postgres duckdb sqlite; do
  $PY bench_databases.py --doc data/medium.json --engines $e \
      --out runs/read_medium_$e.json 2>&1 | tee runs/read_medium_$e.log
done

# 4.3 kv 解耦全套(8 档:锚点×2、结构扫描×2、$in、clustered $in、$lookup×2)
#    脚本默认剩余磁盘 <30 GB 拒跑(保护共享机);盘小可加 --min-free-gb 10
$PY bench_subset_kv.py --doc data/large.json  --out runs/subset_kv_large.json  2> runs/subset_kv_large.log
$PY bench_subset_kv.py --doc data/medium.json --out runs/subset_kv_medium.json 2> runs/subset_kv_medium.log

# 4.4 (可选,若要求所有表同机)写入 / 算子 / 并发 / 多租户
$PY bench_writes.py      --doc data/medium.json --updates 5000 --inserts 2000 --out runs/write_medium.json
$PY bench_operators.py   --doc data/medium.json --out runs/operators_medium.json
$PY bench_concurrency.py --doc data/medium.json --duration 5 --concurrency 1 2 4 8 16 --out runs/concurrency_medium.json
#   16 核机器并发档到 16 为止即可(64 进程会互挤,曲线形状仍成立但没必要);
$PY bench_extra.py       --doc data/medium.json --out runs/extra_medium.json   # 多租户/批量/删除

# 4.5 (可选)争用稳健性:诊断一节 "16–64 后台进程打点查、尾部几乎不动" 的支撑
$PY bench_subset_contention.py --doc data/large.json --out runs/subset_contention_large.json
```

大数据集各步预计时长(16 核参考):flatten 1–3 min,每引擎 ingest 3–8 min,
测量每档 1–3 min;`bench_subset_kv.py` 全程 ~40–60 min。全套(含可选)约半天。

## 5. 跑完后的自检

- `subset_kv_large.json` 里 `struct_id_cov.explain` 必须是
  `PROJECTION_COVERED` / `docsExamined: 0`(脚本会打印)——不是就说明索引或投影配错了。
- 内部一致性:`ref_view`(FETCH)≫ `ref_id_cov`(覆盖);`struct_id_cov` 是全场最快;
  拆 text 前后(anchor 两行 vs bench_databases 的 mongo 行)P95 接近。
- 倍数 sanity:mongo `struct_id_cov` P95 / postgres `q_subtree` P95 ≈ 1.2–1.6(原机器 1.4)。
- 把 `runs/*.json` + `runs/*.log` + `lscpu`/`free -h` 输出整体传回即可,报告回填在原仓库做。

## 6. 打包清单(从本仓库 `bench/db/` 拷出,总共 <1 MB)

```
gen_pageindex.py  gen_formats.py  FORMATS.md  README.md  REMOTE_RUNBOOK.md
bench_databases.py  bench_writes.py  bench_operators.py  bench_concurrency.py
bench_extra.py  bench_subset_kv.py  bench_subset_contention.py
report.py  run_all.sh
```

不拷:`data/`(重新生成)、`runs/`(远端全新产出)、`report/`(回填在原机器做)、
`bench_subset_opt*.py` 与 `bench_subset_kv_resume.py`(单文档 schema 旧实验与断点续跑,
远端全新跑用不到)。

## 7. 结果回填对照(回来后在原仓库做)

| 报告位置 | 数据来源 |
|---|---|
| 主报告 表4 大数据集各行(PG/DuckDB/SQLite 全部操作;Mongo 点查/子节点/取内容) | `read_large_<engine>.json` |
| 主报告 表4 Mongo 取子树(大) | `subset_kv_large.json → phases.struct_id_cov.stats` |
| 主报告 表4 Mongo 取子树(中,现为 “--”) | `subset_kv_medium.json → phases.struct_id_cov.stats` |
| 主报告 表9 / 研究报告 表(kv 全档,含待补的 $lookup、clustered 行) | `subset_kv_large.json` 各 phase |
| 存储/摄入表、写入表、并发图、多租户表(若重跑) | 对应 `runs/*.json` |
| 图1(make_figures.py)与方法一节的主机描述 | 同上 + `lscpu`/`free -h` |
