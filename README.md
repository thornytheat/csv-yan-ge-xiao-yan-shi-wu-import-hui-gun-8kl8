# csvgate · CSV 严格校验与事务导入回滚

上游每天给一份站点订单 CSV，要求「**全对才入库，错了要知道第几行错哪列**」。
本工具把这份人工核对变成 7 个可复核的后端模块，任一行非法则整份回滚，库内明细行数不变。

使用者：数据平台组入库值班同学。

## 能力

| 模块 | 落地位置 | 做什么 |
|---|---|---|
| 1 上传与编码探测 | `app/encoding.py` | BOM → UTF-8-SIG；严格 UTF-8 失败 → GB18030；都失败 → 400 + 建议。响应记录是否 BOM、前 16 字节摘要、逐次尝试 |
| 2 表头契约（版本化） | `app/contract.py` | 契约按版本注册：`v1-yuan` 金额按元、`v2-cents` 金额按分；必需列缺失=错误；额外列 `reject`/`warn`；列顺序无关；重复列名=错误 |
| 3 行级校验 | `app/rows.py` | 主键严格递增（重复 `duplicate` / 倒序 `out_of_order`）、int/date/decimal/enum、必填、长度上限、日期真实性；金额按版本口径校验（元 `decimal_*` / 分 `cents_*`） |
| 4 分组聚合 | `app/aggregate.py` | 按 `site` 求和，并用三条独立口径自证一致（Decimal 复核按版本口径换算） |
| 5 事务导入 | `app/importer.py` | 单事务写 批次+明细+聚合；异常即 ROLLBACK；(sha256, 契约版本) 重复 → `duplicate_file`，可 `force_new_batch` |
| 6 错误报告 | `app/report.py` | 按错误类型汇总 + 逐行依据（行号/列名/原值/原因）+ 修正建议 |
| 7 契约迁移 | `app/migrate.py` | 盖章历史批次版本、回填明细原值、以明细为准重算站点汇总；单事务一次性切换，失败整体回滚，幂等可重跑 |

编排在 `app/pipeline.py`，路由 `app/main.py` 只做参数解析与渲染。

## 核心规则

1. **编码探测**（证据可查）
   - 前 3 字节是 `EF BB BF` → 按 `utf-8-sig` 解码，BOM 剥离；
   - 否则严格 UTF-8；失败再试 `gb18030`（GBK 超集）；
   - 都失败 → HTTP 400，附三次尝试的失败字节偏移与修复建议。
   - 判定是**确定性**的：纯 ASCII 文件一律算 `utf-8`。
2. **契约版本**（金额：元 → 分）
   - 每次导入都解析出明确的契约版本：表单/参数 `contract_version`，缺省 = `v1-yuan`（兼容旧文件与旧调用）；
   - `v1-yuan`：amount 按元，decimal、最多 2 位小数、0.01 ~ 99999999.99；
   - `v2-cents`：amount 按分，整数、1 ~ 9999999999（与元版上限对齐）；
   - 版本随批次（`batches.contract_version`）与每次上传记录（`import_attempts.contract_version`）落库，结果页/复核页可见；
   - 金额入库统一换算成分（`amount_cents`），并保留原始字符串（`amount_raw`），跨版本汇总可对账；
   - 重复文件判定按 **(sha256, 契约版本)**：同一份字节换口径重传不算重复，各自成批。
3. **行级错误只报第一处**：同一行同一列按「必填 → 长度 → 类型/枚举 → 业务」取第一个失败原因，
   避免一个超长 site 值同时被报成长度错和枚举错、干扰计数。
4. **主键严格递增**：与「上一条主键可解析的行」比较；相等 → `duplicate`，更小 → `out_of_order`，更大 → 通过并更新基线。
5. **日期真实性**：先匹配 `YYYY-MM-DD`，再用 `date.fromisoformat` 判真实存在（`2026-02-30` → `date_invalid`）。
6. **聚合自证**：分组累加 cents、明细单趟线性累加、`Decimal` 原始字符串复核（按版本口径 ×100/×1）、组内行数和=明细行数，四条必须同时成立。
7. **事务与重复文件**
   - 全对：`BEGIN IMMEDIATE` 写批次 → 明细 → 聚合 → `COMMIT`；中途异常 `ROLLBACK`，明细行数保持写入前值。
   - 同 (sha256, 契约版本) 已入库且未勾选 `force_new_batch` → 返回 `duplicate_file`，写入 0 行，返回既有批号。
   - 勾选 `force_new_batch` → 新建批次（`-F2`/`-F3` 后缀），旧批次保留且 `superseded_by` 指向新批次，
     语义是「同一份文件被有意重灌，重灌痕迹可追溯」，不是覆盖删除。
8. **契约迁移**（升级后重算并一次性切换）
   - 启动时自动执行一次（也可 `POST /api/migration` 或复核页按钮手动重跑）；
   - 一个事务内完成：历史批次盖章 `v1-yuan` → 明细回填 `amount_raw` 并按版本复核 `amount_cents` →
     以明细为准重算每个批次的站点汇总与总额 → 落下 `meta.contract_migration` 标记；
   - 任一步失败 → 整体 `ROLLBACK`，库内保留上一份完整结果，不留半新半旧；
   - 幂等：重跑同一迁移返回 `already_applied`，不新增批次、不改动任何行；旧版本批次结果始终可查。

## 启动

```bash
cd batch-20261114b/projects/gb-623
cp .env.example .env          # 可选，默认端口 18212
docker compose up -d --build
curl -s http://127.0.0.1:18212/health
```

- 页面：<http://127.0.0.1:18212/>
- 数据卷：`csvgate-data:/app/data`（SQLite `csvgate.sqlite3`）
- 启动即幂等 seed：`meta.seed_version = seed-2026-11-07.1`，批次 `B-20261107-001`（60 行历史周批次）；
  重复启动不会重复写入。**重建数据库**：`docker compose down -v && docker compose up -d --build`。

## 演示步骤

1. `/workbench` 下载 `normal_500.csv` 并上传（默认 `v1-yuan`）→ 结论「已入库」，得批号、站点聚合表、明细前 20 行。
2. 上传 `broken.csv` → HTTP 422，结论「整体回滚」，列出 5 条逐行错误（重复主键 / 倒序 / 非法日期 / 超长 / 枚举越界）。
   刷新首页或 `/health` 看 `fact_rows` 未变。
3. 上传 `gbk_120.csv` → 编码探测显示 `gb18030`，120 行入库。
4. 契约版本：下载 `cents_240.csv`，契约版本选 `v2-cents` 上传 → 240 行按分口径入库；
   同一份文件误选 `v2-cents` 传 `normal_500.csv` → 422，报 `cents_type` 并提示改用 `v1-yuan`。
5. 再上传一次 `normal_500.csv` → `duplicate_file`，写入 0 行；到 `/review` 点「验证重复导入」看只读判定。
6. 勾选 `force_new_batch` 再传 `normal_500.csv` → 新批次，旧批次标 superseded。
7. `/review` 看「契约迁移」面板：启动时已自动盖章 + 重算 + 一次性切换；点「重跑迁移」返回
   `already_applied`，批次数不变；旧版本批次点开仍可查。

命令行等价（验收脚本用的就是这些）：

```bash
curl -s http://127.0.0.1:18212/health
curl -s -F "file=@sample_data/normal_500.csv" -F "extra_policy=reject" http://127.0.0.1:18212/api/import
curl -s -F "file=@sample_data/cents_240.csv" -F "contract_version=v2-cents" http://127.0.0.1:18212/api/import
curl -s -o /tmp/broken.json -w '%{http_code}\n' -F "file=@sample_data/broken.csv" http://127.0.0.1:18212/api/import
python3 -c "import json;d=json.load(open('/tmp/broken.json'));print(d['report']['total'],[e['code'] for e in d['report']['rows']])"
curl -s http://127.0.0.1:18212/api/contracts          # 已注册契约版本 + 迁移状态
curl -s -X POST http://127.0.0.1:18212/api/migration  # 重跑迁移（幂等）
```

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 当前明细行数 + seed 版本 + 契约迁移状态 |
| POST | `/api/import` | multipart：`file`、`contract_version`、`extra_policy`、`force_new_batch`；200 已入库/重复文件、400 编码失败/未知契约版本、422 校验拒绝 |
| POST | `/workbench` | 页面表单同管道；通过 → 303 跳结果页，拒绝 → 422 直接渲染错误表 |
| POST | `/api/contract-check` | 只检查表头（含 `warn` 策略演练，可带 `contract_version`） |
| GET | `/api/contracts` | 已注册契约版本清单（金额口径说明）+ 迁移状态 |
| GET/POST | `/api/migration` | GET 查迁移状态；POST 执行迁移（幂等，重跑返回 `already_applied`） |
| POST | `/migration/run` | 复核页按钮：执行迁移后 303 回 `/review` |
| POST | `/api/replay-check` | 按 sha256 判定「再次上传会不会重复」 |
| GET | `/api/batches` | 批次列表（含契约版本）+ 统计 |
| GET | `/api/samples/{name}` | 样例下载 |
| 页面 | `/`、`/workbench`、`/result/{batch_id}`、`/result/attempt/{id}`、`/review` | 首页/工作台/结果/错误明细/历史复核 |

## 目录

```text
app/            main.py settings.py pipeline.py seed.py migrate.py
                encoding.py contract.py rows.py aggregate.py importer.py report.py store.py
                templates/ static/
sample_data/    normal_500.csv cents_240.csv gbk_120.csv seed_20261107.csv broken.csv
                contract.json make_samples.py
tests/          test_encoding.py test_rows.py test_importer.py test_api.py
                test_contract_versions.py test_migrate.py conftest.py
Dockerfile docker-compose.yml requirements.txt .env.example
```

## 已知限制

- CSV 解析用标准库 `csv.reader`：支持引号包裹字段，但**行号**按「数据行 + 1」换算，
  字段内含换行时行号会偏移（当前样例与常见导出不含换行字段）。
- 编码只覆盖 UTF-8 / UTF-8-BOM / GB18030；UTF-16/UTF-32 会走到 400 并给出提示，不做自动识别。
- 分隔符固定逗号，不做 `;`/`\t` 自动猜测；SJIS、Big5 等会按 GB18030 误判成功的概率存在（GBK 超集覆盖广），
  需要在契约里约定来源编码。
- 单次上传上限 8MB（约 10 万行量级）；不做流式分片。
- 明细与批次不提供删除接口；重灌靠 `force_new_batch` + `superseded_by` 追溯。
- 契约版本靠导入方显式选择（缺省 `v1-yuan`）：纯整数金额的文件在两个版本下都合法，
  选错版本会差 100 倍——版本会随批次落库，复核页可按版本筛出后重灌修正。
- 契约迁移以明细为准重算汇总；无明细的批次（历史脏数据）会让迁移整体失败并保留旧结果，
  需人工处理该批次后重跑。
