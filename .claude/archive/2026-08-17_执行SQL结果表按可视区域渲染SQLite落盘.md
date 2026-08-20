# 执行SQL结果表改为按可视区域渲染 —— SQLite 落盘 + 窗口 LRU

> 2026-08-17。前置：execute_sql 结果表原用内存 `List[List[Any]]` 驻留全部结果行，
> 靠 `MAX_RESULT_ROWS=200k` 截断防 OOM（2M+ 结果会被截断、不可完整浏览）。
> 用户追问"execute_sql 里的属性表格有没有按可视区域渲染，避免 OOM"。

## 关键差异（为什么不能直接复用属性表的方案）

属性表能"只存 fid + 按需 `getFeature(fid)`"是因为 QGIS 图层支持**按 fid 随机访问**。
ogr SQL 结果是**只前向**的流，没有按行号重取的能力——要按可视区域渲染，
必须先把结果落盘到**可随机访问**的介质，再从里面按行号读可视窗口。

## 方案（`app/ui/dialogs/execute_sql.py`）

`VectorLayerTableModel` 改为 **SQLite 落盘 + 可视窗口 LRU**：

- 构造时 `tempfile.mkstemp` 建临时 sqlite（`PRAGMA journal_mode=OFF` + `synchronous=OFF`，
  一次性临时数据免 journal）。
- `append_rows`：worker 的 chunk 经信号回到**主线程**，`executemany` 事务写入 + 逐批
  `commit`（5000 行 ≈ 几 ms），`_n` 递增驱动 `rowCount` 渐进增长（流式 UI 不变）。
- `data(row,col)`：LRU 行缓存（`_cache`，dict 保持插入序），未命中按 `rowid = row+1`
  `SELECT` 单行；`MAX_CACHE_ROWS=500`，超出淘汰最久未用——**内存恒定不随结果总量增长**。
- `set_schema([])`：DROP TABLE 重置（同一临时文件跨多次查询复用）。
- `cleanup()`：关连接 + 删临时文件；`ExecuteSqlDialog.closeEvent` 调用（幂等）。
- worker 不变（仍分批冲刷信号）；`MAX_RESULT_ROWS` 200k → **5M**，语义从"内存截断"
  改为"**磁盘**截断兜底"（防意外笛卡尔积把磁盘写爆；内存不受此限制，正常 2M+ 不触发）。
- 模块顶部 `QLineEdit` 等死导入仍保留（本文件未动 UI 部分）。

## 验证（`_probe_sql_sqlite.py` 已删）

- **模型级 1M 行 × 10 列**：分块 append_rows 4.94s 落盘 102MB；跨全范围 data() 正确；
  浏览 3003 行后缓存恒 **500 ≤ 500**；cleanup 删文件。
- **端到端 120k shapefile** `select *`：表格填满 120k 行，表头 `['name','v']`，
  行 0/60000/119999 取值正确，状态"执行完成，共 120000 条"，关窗删除临时文件。
- lint：`ruff --select F,E9,I` 干净。无既有测试文件；探针代验证。

## 探针踩坑

- `layer.path_c` 是 main_window 动态挂的属性（非 QGIS 内置），探针里需手动赋值。
- `GetFieldAsString` 对 double 返回全精度（`0.000000000000000`）——既有行为，未改。

## 测试运行

`cmd //c "D:/zsh/app/qgis4.2/bin/python-qgis.bat -m ruff check --select F,E9,I app/ui/dialogs/execute_sql.py"`

（本次改动无独立测试文件；属性表窗口缓存改动见 `2026-08-17_属性表只查看可视区域窗口缓存.md`）
