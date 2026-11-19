# 深度登记 光伏电站限电事件与损失电量复算

规模：中型。使用者：新能源场站运营分析员。核心规则：仅用有效对照逆变器和相同辐照区间拟合基线；重叠限电事件先求并集再积分。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 遥测采信 | 执行并复核遥测采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 设备映射 | 执行并复核设备映射 | `POST /api/devices` | `app/modules/devices/rules.py` | `tests/test_devices.py` |
| M3 事件切片 | 执行并复核事件切片 | `POST /api/slicing` | `app/modules/slicing/rules.py` | `tests/test_slicing.py` |
| M4 基线拟合 | 执行并复核基线拟合 | `POST /api/baseline` | `app/modules/baseline/rules.py` | `tests/test_baseline.py` |
| M5 损失核算 | 执行并复核损失核算 | `POST /api/loss` | `app/modules/loss/rules.py` | `tests/test_loss.py` |
| M6 能量对账 | 执行并复核能量对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 运行归档 | 执行并复核运行归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |
| M8 身份台账 | 登记/确认/撤回换型链路，跨编号连续追溯 | `POST /api/identity` | `app/modules/identity/rules.py`、`app/identity_db.py` | `tests/test_identity.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码中文状态 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_source` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_source` |
| TRAP-04 | 分段表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_segment_header` |
| TRAP-05 | 缺列记录 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bad_column` |
| TRAP-18 | 空态 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_three_empty_types` |
| TRAP-07 | 遥测哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/devices/rules.py` | `tests/test_devices.py::test_sentinel_real` |
| TRAP-14 | 设备时刻冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/devices/rules.py` | `tests/test_devices.py::test_conflict_both_isolated` |
| TRAP-15 | 功率单位 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/devices/rules.py` | `tests/test_devices.py::test_megawatt_conversion` |
| TRAP-06 | 全角千分位 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/devices/rules.py` | `tests/test_devices.py::test_fullwidth_number` |
| TRAP-11 | UTC归一 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/devices/rules.py` | `tests/test_devices.py::test_time_equivalence` |
| TRAP-23 | 限电区间并集 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/slicing/rules.py` | `tests/test_slicing.py::test_overlapping_real` |
| TRAP-31 | 无据先并导致相邻设备误并 | 直接把新旧编号合并成一台 | 待证后并：先登记待确认链路，凭证+资产+邻位+时段+互斥五类校验全过才单事务确认 | `app/modules/identity/rules.py`、`app/identity_db.py` | `tests/test_identity.py::test_confirm_rejected_when_evidence_incomplete` |
| TRAP-32 | 重试产生重复身份或覆盖原编号 | 每次登记新建身份、回写批次 | 确定性 LNK/PHY 编号幂等登记；台账与 runs 表物理隔离、只读批次 | `app/identity_db.py` | `tests/test_identity.py::test_register_is_idempotent`、`tests/test_identity.py::test_original_codes_and_runs_never_mutated` |
| TRAP-33 | 切换失败留下半合并 | 分步写入中途失败 | 确认/撤回在单个事务内完成，失败回滚并追加驳回审计，无中间态 | `app/identity_db.py` | `tests/test_identity.py::test_failed_confirm_leaves_no_half_merged_state` |
| TRAP-34 | 凭证撤回引发全量重算 | 重算所有设备身份 | 仅重算受影响 `identity_chains`，其他链路 digest 不变 | `app/identity_db.py` | `tests/test_identity.py::test_withdraw_splits_histories_and_recomputes_only_affected` |
| TRAP-35 | 不同设备共用履历 | 编号被多条已确认链路复用 | CODE_BOUND 互斥校验，运行时段重叠 OVERLAP_RUNTIME 驳回 | `app/modules/identity/rules.py` | `tests/test_identity.py::test_confirmed_code_cannot_join_another_identity`、`tests/test_identity.py::test_overlapping_runtime_cannot_be_same_physical` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。
