# 粉料仓存量（水泥 / 粉煤灰）自动核账设计

## 1. 背景与问题

水泥、粉煤灰两个仓的存量平时只在补料时人工录入一次，白班连续打几十盘后仓量
不再可信，容易把仓打空。需要：

1. 每盘投料后按**当前配方**自动下扣仓量，并据此估出**还能打多少盘**；
2. 接近安全线时**提前报警**；到线是拦还是只报，按仓和标号区分；
3. 称重回读缺失时属于**部分失败**：不能按估数硬扣，必须标不可信、等人工确认；
4. 补料、换仓前后的账可核对，扣减与补料重复触发必须**幂等**；
5. 仓量结论同时供**控制台展示**与**审计对账**，两边口径一致。

## 2. 定死的规则（不再随调用变化）

| 议题 | 定死的口径 |
| --- | --- |
| 安全线按什么判 | **按仓单独判**。安全线是每个仓的物理最低存量（kg），与标号无关，建仓时录入。 |
| 到线后拦还是报 | **按（仓 × 标号）策略分别定**：`block`（拦投料）或 `warn`（只报警放行）。未配置的（仓，标号）组合一律按 `block` 兜底，故障安全。 |
| 还能打多少盘 | 按**当前选中配方**（或指定配方/标号）与每盘方量折算，统计能停在安全线**及以上**的整盘数：`floor((存量 − 安全线) / 每盘用量)`；全厂盘数取各喂料仓最小值。 |
| 提前量 | 每（仓，标号）可配 `warn_lead_batches`（默认 3 盘）；剩余盘数进入提前量区间即报警。 |
| 硬底线 | 任何策略下都不允许把仓扣成负数；`warn` 仓可以越过安全线继续打，但越过零位的投料一律拦。 |
| 回读缺失 | **部分失败**。估数只登记为待确认（pending），**绝不下扣**；该仓立即置为“不可信”，后续该仓投料、补料、换入全部拦住，直到人工确认或迟到的有效回读补入。 |
| 幂等 | 每个写操作必须带 `request_id`；此外扣减按自然键（盘号+物料）、补料按（仓+送货单号）在台账层去重，进程重启后仍成立。重复触发返回原结果，不动账。 |
| 唯一事实源 | 每次变更后发布一份 `stock.balance` 规范快照（含摘要 digest）。控制台只读这份快照；审计从原始 journal 独立重算后与该快照比对。 |

## 3. 记录模型（append-only journal）

所有记录复用现有 `store` 的 `group.leaf` 提交流，origin 为 `stock`：

| kind | 含义 | 关键载荷 |
| --- | --- | --- |
| `stock.silo` | 建仓/录入期初 | silo, material, opening_g, safety_g |
| `stock.policy` | （仓×标号）到线动作 | silo, grade, action=block/warn, warn_lead_batches |
| `stock.draw` | 回读确认的硬扣减 | batch_id, silo, material, grams_g, estimated_g, grade |
| `stock.pending` | 回读缺失的部分失败 | pending_id, batch_id, silo, estimated_g, reason |
| `stock.resolve` | 待确认落账（人工/迟到回读） | pending_id, grams_g, source=manual/scale, accepted_estimate |
| `stock.refill` | 补料 | ticket, mode=additive/measured, delta_g, level_g |
| `stock.route` | 换仓（物料→喂料仓） | material, silo, old_silo, transfer_g |
| `stock.balance` | 规范存量快照（fold 输出） | version, digest, routes, silos[] |

- 内部一律以**整数克**记账（`stock/units.py`），避免浮点丢质量；对外仍以 kg 展示。
- 台账 `StockLedger` 只通过折叠上述记录得到；`adopt()` 在重启时从已提交流重建，
  与在线过程走**同一条 fold 路径**，所以在线与重启后的账必然一致。
- 待确认序号 `pend-000001` 由 `SequenceIds` 生成，重启时恢复计数器，不复用。

## 4. 关键流程

### 4.1 一盘投料 `stock.dose`

1. `request_id` 去重；再按自然键（盘号，物料）查是否已记（已扣或已待确认）→ 幂等返回；
2. 回读有效（`actual_kg` + `readback=scale`）：按本次实投量投影
   `投后存量 = 存量 − 实投`：
   - 仓不可信 → 拦；
   - 投后 < 0 → 拦（硬底线）；
   - 投后 < 安全线且策略 block → 拦；
   - 其余写 `stock.draw` 硬扣减，并按提前量/越线报警。
3. 回读缺失/错误（`readback=missing|error` 或无 `actual_kg`）：
   - 仓已有未决 pending → 拦（不得叠加第二个估数）；
   - 否则写 `stock.pending`，存量不动，发 `stock.readback_missing` 报警，仓置不可信。

### 4.2 待确认的两种收尾

- `stock.readback`：迟到但有效的秤读数补入，按实值硬扣，恢复可信；
- `stock.confirm`：人工处理。必须给 `actual_kg`，或显式 `accept_estimate=true`
  才允许按估数落账——机器永远不能自行按估数扣。确认后硬扣减并恢复可信。
- 对同一个 pending 的重复确认按自然键幂等返回。

### 4.3 补料 `stock.refill`

- 必须带唯一送货单号 `ticket`（按仓幂等，重启仍去重）；
- 仓存在未决 pending 时**拒绝补料**，必须先清账，防止补料掩盖部分失败；
- 两种模式：
  - `additive`：按实送量累加（`delta_g`）；
  - `measured`：以实测料位直接重置（记录重置前后差额，便于盘点核对）。

### 4.4 换仓 `stock.route`

- 只改“物料 → 喂料仓”的指向；目标仓必须实际装有该物料且账可信；
- 换仓带的残余量 `transfer_g` 在新旧仓之间显式搬运，换仓前后**总质量守恒**；
- 换出/换入涉及的仓若有未决 pending 一律拒绝。

### 4.5 查询

- `stock.forecast`：按配方与每盘方量返回每仓存量、安全线、可打盘数、是否可信、
  block/warn、提前报警；全厂 `safe_batches` 为可信仓最小值，任一喂料仓不可信则为 `null`。
- `stock.precheck`：下一盘开投前的闸门结论（allowed + 明细）。
- 控制台 `runtime.state().subsystems.stock` 同时给出规范 balance 与当前配方 forecast。

## 5. 控制台与审计一致性

`stock/reconcile.py` 的对账逻辑：

1. 从 journal 的已提交记录**重新折叠**一个全新 `StockLedger`；
2. 比三件事：活账 digest vs 重算 digest；最新 `stock.balance` 快照 digest；
   各仓 level/safety/trusted/material 与待确认集合；
3. 全部一致才返回 `consistent=true`；任一不符列出 mismatch（如 `digest`、
   `silo-1:level_g`、`balance_snapshot_stale`）并发 `stock.reconcile_failed` 报警，
   **不做静默修复**。

digest 为各仓 `名称|物料|余额|安全线|可信|待确认` 排序行的 sha256。

## 6. 命令一览

```
stock.register   建仓/录期初（silo, material, opening_kg, safety_kg, request_id）
stock.policy     配（仓×标号）动作（action, warn_lead_batches）
stock.forecast   按配方估可打盘数（grade/recipe, quantity）
stock.precheck   下一盘投料前闸门
stock.dose       一盘投料（有效回读硬扣 / 回读缺失挂 pending）
stock.readback   迟到有效回读补落账
stock.confirm    人工确认 pending（实值或显式接受估数）
stock.refill     补料（additive / measured，带 ticket）
stock.switch     换仓（material→silo，可带 transfer_kg）
stock.reconcile  活账与 journal 对账
```

## 7. 模块文件

- `mixplant/stock/units.py`：kg↔整数克
- `mixplant/stock/policy.py`：安全线动作（按仓判线、按仓×标号定动作）
- `mixplant/stock/forecast.py`：可打盘数估计与 block/warn 判定
- `mixplant/stock/ledger.py`：append-only 台账 fold、待确认、幂等键、digest
- `mixplant/stock/reconcile.py`：独立重算与一致性核对
- `mixplant/stock/service.py`：命令服务（校验→提交→折叠→报警）
- 装配：`mixplant/runtime.py`（命令面、`bootstrap()` 中 `stock.adopt()` 回放）
- 测试：`tests/test_stock.py`、`tests/test_stock_restart.py`

## 8. 与既有 cement 服务的关系

既有 `cement` 服务负责闸门/流量工艺记录，其内存 `InventoryLedger` 不具备预估、
部分失败、幂等与对账能力。本 `stock` 子系统是仓存量的**权威账**。上线接线时让
盘秤的有效回读同时驱动 `stock.dose`、回读缺失走 pending；`cement.close_gate`
原有的简单台账保留不动，避免影响既有工艺联锁测试。
