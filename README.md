# 普惠旅游公共服务账本

本项目提供普惠旅游公共服务账本所需的领域事件交换约定、基础校验库与领域规则库。各接入方使用统一的聚合标识、事件版本和发生时间表达业务事实，避免跨系统交换时丢失来源顺序；领域规则在事实快照之上做纯判定，支撑“按公共价值而非单纯客流营收拨款”的复盘。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：单事件中文联调样例。
- `data/ledger_sample.json`：覆盖完整复盘流程的账本样例（游客中心挤占居民休憩空间、淡季低利用的无障碍接驳兜底、故障替代与恢复、三类资金分账、冻结后评价决定）。
- `src/inclusive_tourism_services/contracts.py`：不依赖第三方包的交换层校验器。
- `src/inclusive_tourism_services/ledger.py`：不依赖第三方包的领域规则库。
- `tests/`：契约边界检查与领域规则测试。

## 聚合与事件

聚合类型：service_policy（政策版本与服务承诺）、public_facility（设施与容量）、operation_contract（运营合同与履约证据）、benefit_entitlement（优惠授权与核销）、outage_record（故障停用与替代）、transport_link（交通衔接）、usage_observation（聚合使用观测）、feedback_record（聚合反馈）、fund_account（资金账户）、performance_period（评价期）。

事件类型：POLICY_VERSIONED、SERVICE_COMMITTED、FACILITY_REGISTERED、CAPACITY_REPORTED、BOOKING_RECORDED、CONTRACT_SIGNED、EVIDENCE_SUBMITTED、BENEFIT_GRANTED、BENEFIT_REDEEMED、OUTAGE_DECLARED、ALTERNATIVE_ARRANGED、OUTAGE_RESOLVED、TRANSPORT_LINKED、USAGE_AGGREGATED、FEEDBACK_COLLECTED、FUNDS_ALLOCATED、SUBSIDY_PAID、PERFORMANCE_FROZEN、DECISION_MADE。

## 领域规则（ledger.py）

- **容量保护** `check_capacity` / `capacity_violations`：公益清单保留容量先到先得，商业预约只能在非保留余量内接受，挤占部分计为违规。
- **资金分账** `breakdown_funds` / `fund_accounting_issues`：财政资金、购买服务、经营收入三类来源分别核算；补贴只能出自公共资金，不得混入经营收入账户。
- **优惠核验** `verify_benefits` / `benefit_violations`：消费券、减免、优先服务按资格与生效期核验；同一受益人同一服务只允许在一个渠道核销一次，跨渠道重复即违规。
- **故障连续性** `assess_outages` / `outage_violations`：故障或应急关闭只暂停受影响服务、必须触发替代安排；恢复后就同一故障重复申领补贴即违规。
- **履约边界** `evidence_scope_violations`：运营方只能提交自身合同范围内设施与服务的履约证据。
- **评价治理** `freeze_period` / `decision_governance_violations`：评价人员先冻结数据版本（规范化哈希摘要），决定必须基于冻结摘要作出；利益相关者必须回避，决策人须在冻结时登记的评价人员名单内。
- **隐私** `privacy_violations`：居民使用与游客反馈只以不低于最小群体规模 k（默认 5）的分组计数呈现，低于 k 的桶不得外显，以防暴露个人行踪。
- **复盘洞察** `score_services` / `review_findings` / `investment_journey_explanations`：综合覆盖、公平、可达、质量与淡旺季使用打分；识别居民负担增加（resident_burden_increased）、淡季低效闲置（low_season_idle）与不可替代兜底（safety_net_low_utilization）、待维护短板（maintenance_gap）；把每笔公共投入关联到它改善的行程阶段与受益群体，且不含任何个人标识。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
