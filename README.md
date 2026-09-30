# 红价签 · 超市电子价签批量改价与回滚核验系统

演示一套完整的电子价签（ESL）批量改价运营链路：调价清单导入 → 差异校验 →
审批分离 → 批次生成 → 定时生效 → 模拟价签回执（成功/离线/失败）→
失败项单独重试 → 现场抽检核验 → 促销到期自动恢复 → 紧急版本回滚（幂等）。

仅使用 Python 3.10+ 标准库（`http.server` + `sqlite3` + `threading`），无任何
外部依赖，无需安装、无需网络。

## 一条命令启动

```bash
python3 run.py
# 打开 http://127.0.0.1:8000
```

启动时会重置并重建演示数据，终端依次打印 9 个案例的执行结果（成功、离线、
部分成功、促销恢复、回滚幂等、现场不一致、校验拒绝、审批分离、互斥、促销重叠），
随后启动 Web 后台与进程内定时任务（每 3 秒检查到点生效 / 促销到期恢复）。

其他参数：

```bash
python3 run.py --check       # 只跑 6 项自动化测试
python3 run.py --no-demo     # 保留现有数据库直接启动
python3 run.py --port 8080
```

## 演示数据

- 3 家门店（S001 中山路旗舰店 / S002 滨江社区店 / S003 高新区科技店）
- 8 个电子价签，其中 **ESL-B-0201 长期离线**；6 种商品，均带价格上下限
- 运营 `u_yang 杨小运`、`u_wu 吴启航`；店长 `u_zhou 周敏`（唯一可审批账号）

## 关键业务规则（均有独立结果码，见 `app/results.py`）

| 场景 | 结果码 | 拦截位置 |
|---|---|---|
| 清单内重复商品 | `DUPLICATE_PRODUCT` | 差异校验 |
| 新价超出商品限价 | `PRICE_LIMIT` | 差异校验 |
| 价签编号 / 门店 / 商品不存在 | `TAG_NOT_FOUND` 等 | 差异校验 |
| 与在途批次改同一门店同一商品 | `MUTEX_CONFLICT` | 差异校验 + 审批复检 |
| 促销时段与已批准促销重叠 | `PROMO_OVERLAP` | 差异校验 |
| 审批人审批自己提交的清单 | `SELF_APPROVAL` | 审批 |
| 价签离线 | `ACK_TAG_OFFLINE` | 下发回执 |
| 模拟通信失败（重试可成功） | `ACK_SIMULATED_FAIL` | 下发回执 |
| 部分成功 | 批次 `partial` + 分段条 | 批次 |
| 现场价格不一致 | `INSP_MISMATCH` | 抽检 |
| 回滚目标版本不存在 | `RB_VERSION_MISSING` | 回滚 |
| 重复回滚 | `RB_NO_NEED`（空操作） | 回滚 |

**两价模型**：每个价签同时维护 `expected_price`（系统期望价）与
`display_price`（价签实际显示价）。批次到点时期望价立即前进并在
`price_versions` 追加版本；只有收到价签成功回执，显示价才推进到该版本。
离线/失败标签因此天然呈现两价偏离，总览页与价签台账会高亮。

**互斥**：批次条目从生成起持有 `(门店, 商品)` 锁；普通价成功即释放，
促销价锁持有到促销到期恢复批次完成；紧急回滚撤销未生效条目并释放全部锁。

**回滚**：每个批次条目记录 `old_version / old_price`，回滚严格指向改价前的
明确历史版本（`rollback` 类型的新版本）。若当前价已等于目标价则记为空操作，
**重复执行不新增价格版本、不改变任何价格**。

## Web 页面

- `/` 总览：KPI、最近批次分段进度条、期望价/显示价偏离表
- `/imports/new` 上传调价 CSV（`tag_no,new_price`，可选促销结束时间）
- `/imports/<id>` **商品差异表**：固定「旧价 / 新价 / 生效时间」列，一行一结果
- `/approvals` 审批队列（必须由非提交人的店长审批）
- `/batches`、`/batches/<id>` **分段条**（深灰=成功、浅灰=等待、零售红=失败），
  失败项单独重试 / 一键重试 / 紧急回滚，附本批次全部价格版本
- `/inspect` 现场核验：**大号扫码入口**，价格大号**等宽排版**逐位核对，可上传
  抽检照片（存 `data/photos/`）
- `/rollbacks`、`/rollbacks/<id>` 回滚单与每条价签的版本走向、空操作留痕
- `/tags` 价签台账：在线状态、期望价、显示价、展示版本、限价区间

界面为白色底 + 零售红（#D7263D）+ 深灰（#2B2B2E）三色主题。

## 自动化测试（`python3 run.py --check`）

1. `test_01_batch_mutex` 批次互斥：导入标记 + 审批拒绝 + 回滚释放后放行
2. `test_02_promo_overlap` 促销时段重叠：重叠拒绝 / 他标签不受影响
3. `test_03_approval_separation` 审批分离：提交人、非店长均不能审批
4. `test_04_partial_failure_and_retry` 部分失败 + 失败项单独重试 + 两价偏离保留
5. `test_05_rollback_points_to_version_and_idempotent` 回滚指向明确版本且重复执行幂等
6. `test_06_validation_result_codes` 重复/越限/不存在/空变化各自独立结果

## 目录结构

```
run.py                 启动入口（演示 + 定时任务 + Web）
app/
  db.py                SQLite 建表、演示数据、时间工具
  results.py           全部独立业务结果码与中文标签
  importer.py          CSV 导入与差异校验
  services.py          审批 / 批次下发 / 重试 / 促销恢复 / 回滚 / 抽检
  adapter.py           模拟 ESL 网关（在线/离线/首帧失败回执）
  scheduler.py         进程内定时任务线程
  server.py            Web 后台（页面与 API）
  demo.py              启动时的 9 个演示案例编排
tests/test_system.py   自动化测试
data/
  esl.db               SQLite 价格版本库（运行时生成）
  imports/             上传的调价清单原件
  photos/              现场抽检照片
```
