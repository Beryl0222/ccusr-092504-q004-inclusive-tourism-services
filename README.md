# 普惠旅游公共服务账本

本项目提供普惠旅游公共服务账本所需的领域事件交换约定、基础校验库与事件流重放规则。各接入方使用统一的聚合标识、事件版本和发生时间表达业务事实；财政资金、政府购买服务与经营收入分账核算，避免"只按客流和营收拨款"把公共价值淘汰掉。

## 目录

- `contracts/domain.schema.json`：领域事件信封、22 类已登记事件、10 类聚合、事件×聚合配对与各事件必填业务字段。
- `data/sample.json`：单事件中文联调样例。
- `data/sample_ledger.json`：贯穿复盘场景的完整事件流（政策两版 → 承诺/合同/容量/衔接 → 优惠与资金 → 故障替代恢复 → 使用反馈 → 冻结回避评价）。
- `src/inclusive_tourism_services/contracts.py`：不依赖第三方包的信封校验器（必填、类型、时区、版本、配对、payload 必填）。
- `src/inclusive_tourism_services/ledger.py`：事件流投影 `Register`、领域规则与汇总视图。
- `tests/`：契约边界检查与领域规则回放测试。

## 聚合与事件

| 聚合 | 事件 |
| --- | --- |
| `service_policy` 政策版本/开放承诺 | `POLICY_PUBLISHED`、`POLICY_VERSIONED`、`SERVICE_COMMITTED` |
| `public_facility` 设施能力/维护 | `CAPACITY_REPORTED`、`OUTAGE_DECLARED`、`ALTERNATIVE_ARRANGED`、`SERVICE_RESUMED`、`MAINTENANCE_LOGGED` |
| `operating_contract` 运营合同/证据 | `CONTRACT_AWARDED`、`EVIDENCE_SUBMITTED` |
| `transit_link` 交通衔接 | `LINK_OPENED`、`LINK_CHANGED` 及停用/替代/恢复 |
| `benefit_entitlement` 券/减免/优先 | `BENEFIT_GRANTED`、`BENEFIT_REDEEMED` |
| `funding_ledger` 三本资金账 | `FUNDING_ALLOCATED`、`FUNDING_SETTLED` |
| `usage_record` / `feedback_record` 居民使用与游客反馈 | `USAGE_RECORDED`、`FEEDBACK_COLLECTED` |
| `performance_period` / `review_decision` 评价期与决定 | `PERFORMANCE_FROZEN`、`REVIEWER_ASSIGNED`、`REVIEW_DECIDED` |

## 入账规则（ledger.replay 重放时执行）

1. **公益容量不被挤占**：容量上报必须满足 `公益配额 + 商业配额 = 总容量`，商业预约超出商业配额即违规。
2. **优惠核验**：消费券、减免、优先服务按资格规则与生效期核验；核销渠道必须在优惠目录登记；`dedup_key` 相同即同一受益跨渠道重复计算。
3. **资金分账**：`fiscal`（财政拨款）、`purchase_of_service`（政府购买服务）、`operating_revenue`（经营收入）分别核算。
4. **故障最小影响**：停用只暂停 `affected_service_codes` 内的服务；停用期间必须有替代安排；恢复范围不得超出停用单。
5. **补贴不重复**：停运补偿显式挂接 `outage_id`，同一停用单最多补偿一次，恢复日及之后再结算即重复补贴。
6. **运营方自律**：只能提交本 `operator_id` 合同、且服务在合同范围内、日期在合同期内的履约证据。
7. **冻结后不可改写**：评价期冻结（含 `dataset_hash`）后写入的使用/反馈一律拒收；评价决定必须引用冻结哈希。
8. **利益回避**：评价人员须完成冲突核查，在营运营方不得担任评价人。
9. **五维综合评价**：决定必须给出 coverage（覆盖）、fairness（公平）、accessibility（可达）、quality（质量）、seasonal_use（淡旺季使用）。
10. **兜底保护**：列入公益清单且依赖活跃无障碍衔接的服务，在没有第二条可替代衔接时，不得以淡季低利用率为由撤线。
11. **隐私保护**：使用与反馈只接受聚合数据；反馈单元格少于 5 人拒收；禁止身份证、手机号、个人 ID、行踪轨迹等字段。

重放不静默丢弃问题：所有违规进 `Register.violations`，管理视图（`findings()`、`low_utilization_services()`、`maintenance_backlog()`、`explain_investment()`）在此基础上识别居民负担增加、低效闲置、待维护短板，并解释每笔投入改善了谁的完整行程。

## 复盘样例

`data/sample_ledger.json` 中：

- **城东游客中心（SVC-VC-01）**：旺季接待 3.86 万游客、饱和度 0.95，但商业预约 920 > 商业配额 800 挤占公益容量，居民休憩空间被占、居民满意度 0.52，无障碍坡道整修逾期 → 五维评价公平维度仅 0.45，决定 **rectify（限期整改）**。
- **无障碍接驳线（SVC-SHUTTLE-B1）**：淡季饱和度 0.11，但承担残障与老年居民就医兜底；故障期间仅暂停该线并启用预约出租车，停运补偿在恢复前一次性结算，恢复后替代退出 → 决定 **continue（保留并优化时刻）**。
- 样例同时保留三条被账本拦截的反例：商业挤占容量、跨渠道重复核销、冻结后补报数据。

## 用法

```python
import json
from inclusive_tourism_services.contracts import validate_event
from inclusive_tourism_services import replay, low_utilization_services

schema = json.load(open("contracts/domain.schema.json", encoding="utf-8"))
events = json.load(open("data/sample_ledger.json", encoding="utf-8"))

for e in events:
    assert validate_event(e, schema) == []

reg = replay(events)
assert not {v.rule for v in reg.violations} - {
    "public_quota_encroachment", "benefit_dedup", "frozen_version"
}
print(reg.funding_totals())            # 三类资金分账总额
print(low_utilization_services(reg))   # 淡季低效清单及兜底标记
```

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
