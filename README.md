# 超市电子价签批量改价与回滚核验系统（ESL Demo）

纯 Python 标准库实现（无第三方依赖）：SQLite 记录价格版本，本地目录存放导入文件与抽检照片，
进程内定时任务负责“到点生效 / 促销到期恢复”，模拟适配器返回价签回执。

## 一条命令启动

```bash
python3 run_demo.py
```

它会依次：重置演示数据 → 运行自动化自测（47 项断言）→ 自动生成
**成功 / 部分失败待重试 / 价签离线 / 促销到期恢复 / 紧急回滚(含幂等) / 定时生效 / 现场不一致**
案例 → 启动本地后台 <http://127.0.0.1:8000>。

常用参数：

```bash
python3 run_demo.py --test-only     # 只跑测试
python3 run_demo.py --no-test       # 跳过测试直接造数启动
python3 run_demo.py --port 8080
```

## 页面

| 路径 | 内容 |
| --- | --- |
| `/` | 总览：每枚价签的**实际显示价**与**系统期望价**并排，离线/漂移高亮 |
| `/imports` | 上传或粘贴 CSV，差异校验表**固定旧价 / 新价 / 生效时间**列 |
| `/approvals` | 审批队列，仅通过项在审批后生成批次；提交人自审会被拒绝 |
| `/batches` | 批次**分段进度条**：深灰=成功、浅灰=等待、零售红=失败；失败项可单项重试 |
| `/inspect` | 现场核验：**大号扫码入口**，期望/实际价用**等宽数字**逐位核对，可传照片 |
| `/rollback` | 价格版本链，紧急回滚必须指向明确版本，重复回滚只记 `idempotent_skip` |

配色：白色背景、零售红 `#D7001A`、深灰 `#2A2A2E`。

## 导入清单格式

`data/imports/` 存放落盘的导入文件。列：

```
store_id,product_id,new_price,effective_at,promo_start,promo_end
S001,100001,1.80,now,,
S001,100004,59.90,2026-10-01 09:00:00,2026-10-01 09:00:00,2026-10-07 22:00:00
```

`effective_at` 支持 `now`、`+1h` 或 `YYYY-MM-DD HH:MM:SS`。

## 独立结果码

| 结果码 | 触发条件 |
| --- | --- |
| `DUPLICATE_PRODUCT` | 同一清单内重复商品行 |
| `PRICE_LIMIT` | 新价超出当前价 0.30–3.00 倍区间 |
| `TAG_OFFLINE` | 价签处于离线状态 |
| `PROMO_OVERLAP` | 与该商品已有促销时段交叉 |
| `PRODUCT_NOT_FOUND` / `STORE_NOT_FOUND` | 编号不存在 |
| `INVALID_TIME` | 时间无法解析 / 早于当前 / 促销时段不合法 |
| `BAD_NUMBER` | 新价不是有效正数 |
| `BATCH_CONFLICT` | 同门店同商品已有在途批次（pending/active/partial） |
| `APPROVAL_SELF` | 审批人审批自己提交的清单 |
| `ROLLBACK_NO_TARGET` | 回滚未指向明确存在的历史版本 |

## 关键业务规则

- **两份价格永远保留**：`price_versions` + `current_prices` 是系统期望价；
  `tags.displayed_price / displayed_version_id` 是价签实际显示价，仅在回执成功后更新。
- **批次互斥**：两个批次不能同时改同一门店同一商品；导入期与审批生成批次时各校验一次，
  全部明细被排除的空批次直接置 `cancelled` 不占锁。
- **审批分离**：提交人与审批人不能相同。
- **部分成功**：成功项更新展示版本，失败项保留旧展示价并可单独重试；重试不重复追加版本。
- **促销到期**：调度任务为到期促销自动发起 `promo_restore` 批次，恢复到促销前版本且只恢复一次。
- **回滚幂等**：回滚追加指向明确目标的 `rollback` 版本；对同一目标重复执行只写
  `idempotent_skip` 留痕，不产生新版本 / 批次 / 下发。

## 自测覆盖

`python3 -m app.tests` 覆盖：批次互斥、促销时段重叠、审批分离、部分失败重试、回滚幂等，
另含七种结果码、促销到期恢复、现场一致/不一致/离线。

## 目录

```
app/db.py          建表 + 演示数据（门店/货架/商品/价签/初始价格版本）
app/services.py    导入校验 / 审批 / 批次下发 / 模拟适配器 / 重试 / 到期恢复 / 抽检 / 回滚
app/scheduler.py   进程内定时任务（到点生效、到期恢复）
app/server.py      本地 Web 后台（标准库 http.server）
app/tests.py       自动化自测
run_demo.py        一键：自测 + 造案例 + 启动
data/              运行期生成：esl.db、imports/、photos/
```
