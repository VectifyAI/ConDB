# 基准执行目标(SG 腾讯云竞价机,16C/61G/100G)

按 ~/bench/REMOTE_RUNBOOK.md,§4.4/§4.5 可选项已由用户砍掉。测量窗口内本机零其他负载。

## 待办清单(按序,完成打 x)

- [x] §2 环境:docker(condb_pg:55432 / condb_mongo:57017,绑 127.0.0.1)、uv venv 3.10 四包
- [x] §3 数据:medium.json 92.4MB/77,199 节点;large.json 14.06GB/10M 节点/深度8
- [x] §4.1 large 读对比 ✓ 全部完成 17:56(四引擎均吻合旧数据形状)
      (mongo ✓ 17:37;postgres ✓ 17:47 数据与旧报告吻合;duckdb ✓ 17:49 subtree 吻合旧数、点读慢2×(版本差,形状不变);sqlite 进行中 bnucgac5u;mongo 单文档 q_subtree P95 2.4s 异常已报用户,不回填,待 kv 倍数自检裁决)
- [x] §4.2 medium 读对比 ✓ 18:00(PG medium q_subtree 1.78ms vs 旧 0.21ms 偏高已记录,整表替换)
- [x] §4.3 kv ✓ 18:46(large 7 档实测+kv_in_clu 定性;medium 全 8 档)
- [x] §5 自检 ✓ 18:47:covered ✓、内部一致性 ✓、倍数 1.33/1.37 ✓;拆text anchor 差 8.6×=61G 内存下单文档含text不驻留,已向用户说明,不进报告;
      mongo struct_id_cov P95 / postgres q_subtree P95 ∈ [1.2, 1.6];18:12 提前核:PROJECTION_COVERED/docsExamined:0 ✓,P95 比 129.9/97.6=1.33 ✓
- [x] 交付 ✓ 18:52:condb_runs.tar.gz 打包;main.tex 回填推送(5eac59c);两处 NOTE(sg-rerun) 留作者审

## 硬性约束

- bench_databases.py 每次调用必带 --sqlite-path runs/_sqlite.db --duckdb-path runs/_duck.db
- 每引擎测完立即清理并确认盘占用回落,再启动下一个(100G 盘)
- 每轮检查跑 ~/bench/sync_overleaf.sh 推 runs/*.json + 日志到 ~/overleaf_report
- 跑批期间不引入额外负载;检查只 tail 日志
- medium 节点数与旧报告有差异(77,199 vs 70,843),已报用户待确认,不阻塞跑批
- 全部完成后:更新 Overleaf main.tex 两处 TODO(kv-run) + 各表数字 + 方法一节主机描述
  (用户未反对;若用户说自己回填则只推数据)

## 恢复指引(竞价实例回收后)

docker start condb_pg condb_mongo;检查 runs/ 已有哪些 json,从缺的那步接着跑;
kv 脚本中断过就先 drop mongo bench 库再整跑。
