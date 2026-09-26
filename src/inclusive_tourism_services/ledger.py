"""普惠旅游公共服务账本的领域规则。

本模块只做纯领域判断，不触碰传输与持久化：输入为不可变的事实快照
（字典组成的列表），输出为判定结果与问题清单，便于评审、测试与复算。

账本覆盖的规则族：

- 容量保护：公益清单容量不得被商业预约挤占；
- 资金分账：财政资金、购买服务、经营收入分别核算；
- 优惠核验：消费券、减免、优先服务按资格与生效期核验，
  同一受益不得跨渠道重复计算；
- 故障与恢复：只暂停受影响服务并触发替代安排，恢复后不重复补贴；
- 履约边界：运营方只能提交自身合同范围内的履约证据；
- 评价治理：评价人员冻结数据版本后决策，利益相关者回避；
- 隐私：居民使用与游客反馈只以达到最小群体规模的聚合形式呈现；
- 复盘洞察：覆盖、公平、可达、质量与淡旺季使用的综合判定，
  识别居民负担增加、低效闲置与待维护短板。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence

# --- 基础常量 -------------------------------------------------------------

# 三类分账口径，互斥且必须显式标注
FUND_SOURCES = frozenset({"fiscal", "purchase_of_service", "operating_revenue"})

# 财政资金、购买服务属于公共资金；经营收入不得混入公共核算
PUBLIC_FUND_SOURCES = frozenset({"fiscal", "purchase_of_service"})

# 公益清单（免费或低价普惠开放）对应的预约渠道
PUBLIC_LIST_CHANNELS = frozenset({"public_walk_in", "public_booking"})
COMMERCIAL_CHANNEL = "commercial_booking"

# 优惠类型：消费券、减免、优先服务
BENEFIT_KINDS = frozenset({"voucher", "fee_waiver", "priority_service"})

SEASONS = frozenset({"peak", "shoulder", "off"})

# 聚合使用观测的最小群体规模：低于该值的人群桶不得外显，以防暴露个人行踪
DEFAULT_K_ANONYMITY = 5


# --- 通用结果结构 ---------------------------------------------------------


@dataclass(frozen=True)
class LedgerIssue:
    rule: str
    subject: str
    code: str
    message: str


@dataclass(frozen=True)
class CapacityCheck:
    facility_id: str
    total: int
    public_reserved: int
    public_used: int
    commercial_accepted: int
    displaced: int
    """被商业预约挤占的公益席位数（不得大于零）。"""

    @property
    def public_remaining(self) -> int:
        return self.public_reserved - self.public_used


@dataclass(frozen=True)
class FundBreakdown:
    fiscal: int
    purchase_of_service: int
    operating_revenue: int

    @property
    def public_total(self) -> int:
        return self.fiscal + self.purchase_of_service

    @property
    def total(self) -> int:
        return self.public_total + self.operating_revenue


@dataclass(frozen=True)
class BenefitVerification:
    benefit_id: str
    eligible: bool
    within_effective_period: bool
    duplicate: bool
    """是否与既有核销构成同一受益的跨渠道重复。"""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutageAssessment:
    outage_id: str
    suspended_service_ids: tuple[str, ...]
    unaffected_service_ids: tuple[str, ...]
    alternative_service_id: str | None
    resumed: bool
    duplicate_subsidy: bool


@dataclass(frozen=True)
class FreezeManifest:
    period_id: str
    digest: str
    event_count: int


@dataclass(frozen=True)
class ServiceScore:
    facility_id: str
    coverage: float
    fairness: float
    accessibility: float
    quality: float
    seasonality: float
    overall: float
    tags: tuple[str, ...]


# --- 小工具 ---------------------------------------------------------------


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _iterate(facts: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(facts, Iterable) or isinstance(facts, (str, bytes, Mapping)):
        return []
    return [fact for fact in facts if isinstance(fact, Mapping)]


def _details(fact: Mapping[str, Any]) -> Mapping[str, Any]:
    details = fact.get("details", {})
    return details if isinstance(details, Mapping) else {}


def _facts_for(facts: Any, event_type: str) -> list[Mapping[str, Any]]:
    return [f for f in _iterate(facts) if f.get("event_type") == event_type]


# --- 1. 容量保护：公益清单容量不得被商业预约挤占 ---------------------------


def check_capacity(facts: Any) -> list[CapacityCheck]:
    """对每条 CAPACITY_REPORTED 结合预约记录核算挤占情况。

    容量事实的 details 约定：
    total / public_reserved（公益清单保留容量）/ public_used /
    commercial_accepted。公共容量先到先得且不得转售给商业渠道，
    当公共使用与商业接受量之和超过总容量时，超出部分计为挤占。
    """
    checks: list[CapacityCheck] = []
    for fact in sorted(_facts_for(facts, "CAPACITY_REPORTED"), key=lambda f: f.get("aggregate_id", "")):
        d = _details(fact)
        total = int(d.get("total", 0))
        public_reserved = int(d.get("public_reserved", 0))
        public_used = int(d.get("public_used", 0))
        commercial_accepted = int(d.get("commercial_accepted", 0))
        displaced = max(0, public_used + commercial_accepted - total)
        # 商业预约不得占用公益保留容量
        commercial_headroom = total - public_reserved
        displaced = max(displaced, commercial_accepted - max(0, commercial_headroom))
        checks.append(
            CapacityCheck(
                facility_id=str(fact.get("aggregate_id", "")),
                total=total,
                public_reserved=public_reserved,
                public_used=public_used,
                commercial_accepted=commercial_accepted,
                displaced=displaced,
            )
        )
    return checks


def capacity_violations(facts: Any) -> list[LedgerIssue]:
    issues: list[LedgerIssue] = []
    for check in check_capacity(facts):
        if check.displaced > 0:
            issues.append(
                LedgerIssue(
                    rule="capacity_protection",
                    subject=check.facility_id,
                    code="public_capacity_displaced",
                    message=f"商业预约挤占公益清单容量 {check.displaced} 个席位",
                )
            )
        if check.public_reserved > check.total:
            issues.append(
                LedgerIssue(
                    rule="capacity_protection",
                    subject=check.facility_id,
                    code="reserved_exceeds_total",
                    message="公益保留容量大于设施总容量",
                )
            )
    return issues


# --- 2. 资金分账 ----------------------------------------------------------


def breakdown_funds(facts: Any) -> FundBreakdown:
    """按 FUNDS_ALLOCATED 与 SUBSIDY_PAID 的 source 分别汇总金额（单位：分）。"""
    totals = {source: 0 for source in FUND_SOURCES}
    for fact in [*_facts_for(facts, "FUNDS_ALLOCATED"), *_facts_for(facts, "SUBSIDY_PAID")]:
        d = _details(fact)
        source = d.get("source")
        amount = d.get("amount", 0)
        if source in totals and isinstance(amount, int) and not isinstance(amount, bool):
            totals[source] += amount
    return FundBreakdown(
        fiscal=totals["fiscal"],
        purchase_of_service=totals["purchase_of_service"],
        operating_revenue=totals["operating_revenue"],
    )


def fund_accounting_issues(facts: Any) -> list[LedgerIssue]:
    issues: list[LedgerIssue] = []
    for fact in [*_facts_for(facts, "FUNDS_ALLOCATED"), *_facts_for(facts, "SUBSIDY_PAID")]:
        d = _details(fact)
        subject = str(fact.get("event_id") or fact.get("aggregate_id", ""))
        source = d.get("source")
        if source not in FUND_SOURCES:
            issues.append(
                LedgerIssue(
                    rule="segregated_accounting",
                    subject=subject,
                    code="unknown_fund_source",
                    message="资金来源必须是 fiscal / purchase_of_service / operating_revenue 之一",
                )
            )
        amount = d.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            issues.append(
                LedgerIssue(
                    rule="segregated_accounting",
                    subject=subject,
                    code="amount_must_be_positive_integer",
                    message="金额必须是以分为单位的正整数",
                )
            )
    # 补贴只能来自公共资金；经营收入不得用于对外补贴拨付
    for fact in _facts_for(facts, "SUBSIDY_PAID"):
        d = _details(fact)
        if d.get("source") == "operating_revenue":
            issues.append(
                LedgerIssue(
                    rule="segregated_accounting",
                    subject=str(fact.get("event_id", "")),
                    code="subsidy_from_revenue",
                    message="补贴拨付不得混入经营收入账户",
                )
            )
    return issues


# --- 3. 优惠核验与跨渠道去重 ----------------------------------------------


def verify_benefits(facts: Any) -> list[BenefitVerification]:
    """核验 BENEFIT_REDEEMED：资格、生效期，以及同一受益跨渠道重复。

    BENEFIT_GRANTED 的 details 记录 entitlement_id、beneficiary_ref（群体或
    脱敏个人标识）、kind（voucher/fee_waiver/priority_service）、
    eligible_qualification、effective_from、effective_to、channel。

    BENEFIT_REDEEMED 的 details 记录 entitlement_id、beneficiary_ref、kind、
    channel、service_id、redeemed_at。同一 (beneficiary_ref, kind, service_id)
    只允许在一个渠道核销一次。
    """
    grants: dict[str, Mapping[str, Any]] = {}
    for fact in _facts_for(facts, "BENEFIT_GRANTED"):
        d = _details(fact)
        key = str(d.get("entitlement_id") or fact.get("aggregate_id", ""))
        grants[key] = d

    seen: set[tuple[str, str, str]] = set()
    results: list[BenefitVerification] = []
    redemptions = sorted(
        _facts_for(facts, "BENEFIT_REDEEMED"),
        key=lambda f: (str(_details(f).get("redeemed_at", "")), str(f.get("event_id", ""))),
    )
    for fact in redemptions:
        d = _details(fact)
        reasons: list[str] = []
        entitlement_id = str(d.get("entitlement_id", ""))
        grant = grants.get(entitlement_id)

        kind_ok = d.get("kind") in BENEFIT_KINDS
        if not kind_ok:
            reasons.append("优惠类型未登记")

        qualification = d.get("qualification")
        eligibility_ok = bool(grant) and grant.get("eligible_qualification") == qualification
        if grant is None:
            reasons.append("缺少授权记录")
        elif not eligibility_ok:
            reasons.append("资格不匹配")

        period_ok = False
        if grant is not None:
            redeemed_raw = d.get("redeemed_at") or fact.get("occurred_at")
            try:
                redeemed = _parse_date(str(redeemed_raw))
                period_ok = (
                    _parse_date(str(grant.get("effective_from")))
                    <= redeemed
                    <= _parse_date(str(grant.get("effective_to")))
                )
            except (TypeError, ValueError):
                period_ok = False
        if not period_ok:
            reasons.append("不在生效期内")

        key = (
            str(d.get("beneficiary_ref", "")),
            str(d.get("kind", "")),
            str(d.get("service_id", "")),
        )
        duplicate = key in seen
        if duplicate:
            reasons.append("同一受益跨渠道重复计算")
        else:
            seen.add(key)

        results.append(
            BenefitVerification(
                benefit_id=str(fact.get("event_id", "")),
                eligible=kind_ok and eligibility_ok,
                within_effective_period=period_ok,
                duplicate=duplicate,
                reasons=tuple(reasons),
            )
        )
    return results


def benefit_violations(facts: Any) -> list[LedgerIssue]:
    issues: list[LedgerIssue] = []
    for result in verify_benefits(facts):
        if not result.eligible:
            issues.append(
                LedgerIssue(
                    rule="benefit_eligibility",
                    subject=result.benefit_id,
                    code="ineligible_redemption",
                    message="；".join(r for r in result.reasons if "资格" in r or "授权" in r or "类型" in r)
                    or "核销不符合授权",
                )
            )
        if not result.within_effective_period:
            issues.append(
                LedgerIssue(
                    rule="benefit_eligibility",
                    subject=result.benefit_id,
                    code="outside_effective_period",
                    message="核销时间不在优惠生效期内",
                )
            )
        if result.duplicate:
            issues.append(
                LedgerIssue(
                    rule="benefit_dedup",
                    subject=result.benefit_id,
                    code="duplicate_benefit",
                    message="同一受益已在其他渠道核销，不得重复计算",
                )
            )
    return issues


# --- 4. 故障、应急关闭与恢复 ----------------------------------------------


def assess_outages(facts: Any) -> list[OutageAssessment]:
    """评估每条 OUTAGE_DECLARED 的暂停范围、替代安排与恢复后补贴。

    OUTAGE_DECLARED details：affected_service_ids（受影响服务）、
    all_service_ids（设施全部服务）、reason。
    ALTERNATIVE_ARRANGED details：outage_id、alternative_service_id。
    OUTAGE_RESOLVED details：outage_id、resumed_service_ids。
    SUBSIDY_PAID details 可带 outage_id：同一故障恢复拨付只允许一次。
    """
    outages = {
        str(f.get("aggregate_id", "")): f
        for f in _facts_for(facts, "OUTAGE_DECLARED")
    }
    alternatives: dict[str, str] = {}
    for fact in _facts_for(facts, "ALTERNATIVE_ARRANGED"):
        d = _details(fact)
        if d.get("outage_id"):
            alternatives[str(d["outage_id"])] = str(d.get("alternative_service_id", ""))

    resumed: dict[str, set[str]] = {}
    for fact in _facts_for(facts, "OUTAGE_RESOLVED"):
        d = _details(fact)
        resumed.setdefault(str(d.get("outage_id", "")), set()).update(
            str(s) for s in d.get("resumed_service_ids", [])
        )

    outage_subsidies: dict[str, int] = {}
    for fact in _facts_for(facts, "SUBSIDY_PAID"):
        outage_id = _details(fact).get("outage_id")
        if outage_id:
            outage_subsidies[str(outage_id)] = outage_subsidies.get(str(outage_id), 0) + 1

    assessments: list[OutageAssessment] = []
    for outage_id, fact in sorted(outages.items()):
        d = _details(fact)
        affected = frozenset(str(s) for s in d.get("affected_service_ids", []))
        all_services = frozenset(str(s) for s in d.get("all_service_ids", []))
        suspended = tuple(sorted(affected))
        unaffected = tuple(sorted(all_services - affected))
        resumed_set = resumed.get(outage_id, set())
        assessments.append(
            OutageAssessment(
                outage_id=outage_id,
                suspended_service_ids=suspended,
                unaffected_service_ids=unaffected,
                alternative_service_id=alternatives.get(outage_id),
                resumed=bool(resumed_set) and resumed_set >= affected,
                duplicate_subsidy=outage_subsidies.get(outage_id, 0) > 1,
            )
        )
    return assessments


def outage_violations(facts: Any) -> list[LedgerIssue]:
    issues: list[LedgerIssue] = []
    # 故障期间未安排替代服务（兜底服务中断不可接受）
    for assessment in assess_outages(facts):
        if not assessment.alternative_service_id:
            issues.append(
                LedgerIssue(
                    rule="outage_continuity",
                    subject=assessment.outage_id,
                    code="alternative_missing",
                    message="故障或应急关闭必须触发替代安排",
                )
            )
        if assessment.duplicate_subsidy:
            issues.append(
                LedgerIssue(
                    rule="outage_continuity",
                    subject=assessment.outage_id,
                    code="duplicate_recovery_subsidy",
                    message="服务恢复后不得就同一故障重复申领补贴",
                )
            )

    # 暂停范围超出受影响服务
    for fact in _facts_for(facts, "OUTAGE_DECLARED"):
        d = _details(fact)
        affected = frozenset(str(s) for s in d.get("affected_service_ids", []))
        all_services = frozenset(str(s) for s in d.get("all_service_ids", []))
        if not affected <= all_services:
            issues.append(
                LedgerIssue(
                    rule="outage_scope",
                    subject=str(fact.get("aggregate_id", "")),
                    code="suspension_outside_scope",
                    message="只能暂停受故障影响的服务，不得扩大停用范围",
                )
            )
        if not affected:
            issues.append(
                LedgerIssue(
                    rule="outage_scope",
                    subject=str(fact.get("aggregate_id", "")),
                    code="affected_services_required",
                    message="故障申报必须列明受影响服务",
                )
            )
    return issues


# --- 5. 履约证据边界 ------------------------------------------------------


def evidence_scope_violations(facts: Any) -> list[LedgerIssue]:
    """运营方只能提交自身运营合同范围内的履约证据。

    CONTRACT_SIGNED details：operator_id、facility_ids、service_ids。
    EVIDENCE_SUBMITTED details：operator_id、facility_id / service_id。
    """
    operator_scope: dict[str, tuple[set[str], set[str]]] = {}
    for fact in _facts_for(facts, "CONTRACT_SIGNED"):
        d = _details(fact)
        operator = str(d.get("operator_id", ""))
        facilities, services = operator_scope.setdefault(operator, (set(), set()))
        facilities.update(str(x) for x in d.get("facility_ids", []))
        services.update(str(x) for x in d.get("service_ids", []))

    issues: list[LedgerIssue] = []
    for fact in _facts_for(facts, "EVIDENCE_SUBMITTED"):
        d = _details(fact)
        operator = str(d.get("operator_id", ""))
        facilities, services = operator_scope.get(operator, (set(), set()))
        subject = str(fact.get("event_id", ""))
        facility_id = d.get("facility_id")
        service_id = d.get("service_id")
        if operator not in operator_scope:
            issues.append(
                LedgerIssue(
                    rule="evidence_scope",
                    subject=subject,
                    code="operator_without_contract",
                    message="提交证据的运营方没有有效运营合同",
                )
            )
            continue
        if facility_id is not None and str(facility_id) not in facilities:
            issues.append(
                LedgerIssue(
                    rule="evidence_scope",
                    subject=subject,
                    code="facility_outside_contract",
                    message="证据指向合同范围外的设施，运营方不得代为履约证明",
                )
            )
        if service_id is not None and str(service_id) not in services:
            issues.append(
                LedgerIssue(
                    rule="evidence_scope",
                    subject=subject,
                    code="service_outside_contract",
                    message="证据指向合同范围外的服务",
                )
            )
    return issues


# --- 6. 数据冻结与利益相关者回避 -------------------------------------------


def freeze_period(facts: Any, period_id: str) -> FreezeManifest:
    """对评价期内截至冻结事件的全部事实做规范化哈希。

    哈希对字段键排序并覆盖事件的核心标识与发生时间，保证任何事实增删改
    都会改变摘要；评价人员只能在冻结摘要之上作出决定。
    """
    freeze_fact = next(
        (
            f
            for f in _facts_for(facts, "PERFORMANCE_FROZEN")
            if _details(f).get("period_id") == period_id
        ),
        None,
    )
    if freeze_fact is None:
        raise KeyError(f"评价期 {period_id} 尚未冻结")
    frozen_at = str(freeze_fact.get("occurred_at", ""))

    in_scope = [
        f
        for f in _iterate(facts)
        if str(f.get("occurred_at", "")) <= frozen_at
        and _details(f).get("period_id", period_id) == period_id
    ]
    canonical = "\n".join(
        repr(
            (
                str(f.get("event_id", "")),
                str(f.get("event_type", "")),
                str(f.get("aggregate_id", "")),
                int(f.get("version", 0)),
                str(f.get("occurred_at", "")),
            )
        )
        for f in sorted(in_scope, key=lambda x: str(x.get("event_id", "")))
    )
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    return FreezeManifest(period_id=period_id, digest=digest, event_count=len(in_scope))


def decision_governance_violations(facts: Any) -> list[LedgerIssue]:
    """DECISION_MADE 必须基于已冻结版本，且决策人不得是利益相关者。

    PERFORMANCE_FROZEN details：period_id、data_digest、
    reviewer_ids（无利害关系评价人）。
    DECISION_MADE details：period_id、based_on_digest、decider_id、
    interested_party_ids（运营方、受益方等应回避主体）。
    """
    freezes: dict[str, Mapping[str, Any]] = {}
    for fact in _facts_for(facts, "PERFORMANCE_FROZEN"):
        freezes[str(_details(fact).get("period_id", ""))] = fact

    issues: list[LedgerIssue] = []
    for fact in _facts_for(facts, "DECISION_MADE"):
        d = _details(fact)
        subject = str(fact.get("event_id", ""))
        period_id = str(d.get("period_id", ""))
        freeze = freezes.get(period_id)
        if freeze is None:
            issues.append(
                LedgerIssue(
                    rule="decision_governance",
                    subject=subject,
                    code="period_not_frozen",
                    message="作出评价决定前必须先冻结数据版本",
                )
            )
        else:
            frozen_digest = _details(freeze).get("data_digest")
            if d.get("based_on_digest") != frozen_digest:
                issues.append(
                    LedgerIssue(
                        rule="decision_governance",
                        subject=subject,
                        code="digest_mismatch",
                        message="决定所依据的数据版本与冻结版本不一致",
                    )
                )
        decider = d.get("decider_id")
        interested = {str(x) for x in d.get("interested_party_ids", [])}
        if decider is not None and str(decider) in interested:
            issues.append(
                LedgerIssue(
                    rule="decision_governance",
                    subject=subject,
                    code="conflicted_decider",
                    message="利益相关者必须回避，不得参与评价决定",
                )
            )
        reviewers = {str(x) for x in _details(freeze).get("reviewer_ids", [])} if freeze else set()
        if decider is not None and reviewers and str(decider) not in reviewers:
            issues.append(
                LedgerIssue(
                    rule="decision_governance",
                    subject=subject,
                    code="decider_not_among_reviewers",
                    message="决策人不在冻结时登记的评价人员名单内",
                )
            )
    return issues


# --- 7. 隐私：k-匿名聚合 ---------------------------------------------------


def privacy_violations(facts: Any, *, k: int = DEFAULT_K_ANONYMITY) -> list[LedgerIssue]:
    """居民使用（USAGE_AGGREGATED）与反馈（FEEDBACK_COLLECTED）只能以
    达到最小群体规模 k 的人群桶呈现；任何低于 k 的桶都可能暴露个人行踪。
    """
    issues: list[LedgerIssue] = []
    for event_type in ("USAGE_AGGREGATED", "FEEDBACK_COLLECTED"):
        for fact in _facts_for(facts, event_type):
            d = _details(fact)
            buckets = d.get("group_buckets")
            subject = str(fact.get("event_id", ""))
            if not isinstance(buckets, Sequence) or isinstance(buckets, (str, bytes)):
                issues.append(
                    LedgerIssue(
                        rule="privacy_k_anonymity",
                        subject=subject,
                        code="group_buckets_required",
                        message="使用与反馈必须提供分组聚合计数",
                    )
                )
                continue
            for bucket in buckets:
                if not isinstance(bucket, Mapping):
                    continue
                count = bucket.get("count", 0)
                if not isinstance(count, int) or isinstance(count, bool) or count < k:
                    issues.append(
                        LedgerIssue(
                            rule="privacy_k_anonymity",
                            subject=subject,
                            code="bucket_below_k",
                            message=(
                                f"分组 {bucket.get('group', '?')} 样本 {count} 低于最小群体规模 {k}，"
                                "不得外显以防暴露个人行踪"
                            ),
                        )
                    )
    return issues


# --- 8. 复盘洞察 ----------------------------------------------------------


def _service_season_usage(facts: Any) -> dict[str, dict[str, int]]:
    usage: dict[str, dict[str, int]] = {}
    for fact in _facts_for(facts, "USAGE_AGGREGATED"):
        d = _details(fact)
        facility_id = str(d.get("facility_id", ""))
        season = d.get("season")
        count = d.get("total_count", 0)
        if season in SEASONS and isinstance(count, int):
            usage.setdefault(facility_id, {})[season] = (
                usage.setdefault(facility_id, {}).get(season, 0) + count
            )
    return usage


def score_services(facts: Any, *, k: int = DEFAULT_K_ANONYMITY) -> list[ServiceScore]:
    """综合覆盖、公平、可达、质量与淡旺季使用为每个设施打分（0-1）。

    输入指标来自 USAGE_AGGREGATED.details.metrics，各分量已在 0-1 归一：
    coverage（服务半径覆盖人口比）、fairness（居民/游客等群体使用均衡）、
    accessibility（无障碍与重点群体便利达标率）、quality（反馈与履约质量）、
    seasonality（淡季兜底维持度，由淡旺季使用量折算）。
    """
    season_usage = _service_season_usage(facts)
    metrics_by_facility: dict[str, list[Mapping[str, Any]]] = {}
    for fact in _facts_for(facts, "USAGE_AGGREGATED"):
        d = _details(fact)
        metrics = d.get("metrics")
        if isinstance(metrics, Mapping):
            metrics_by_facility.setdefault(str(d.get("facility_id", "")), []).append(metrics)

    scores: list[ServiceScore] = []
    for facility_id, metric_list in sorted(metrics_by_facility.items()):

        def avg(name: str) -> float:
            values = [float(m[name]) for m in metric_list if isinstance(m.get(name), (int, float))]
            return sum(values) / len(values) if values else 0.0

        coverage = avg("coverage")
        fairness = avg("fairness")
        accessibility = avg("accessibility")
        quality = avg("quality")

        seasons = season_usage.get(facility_id, {})
        peak = max(seasons.get("peak", 0), 1)
        off_ratio = min(1.0, seasons.get("off", 0) / peak)
        # 淡季保持一定服务本身即兜底价值，给予基础分
        seasonality = round(0.4 + 0.6 * off_ratio, 4)

        overall = round(
            (coverage * 0.25 + fairness * 0.2 + accessibility * 0.2 + quality * 0.2 + seasonality * 0.15),
            4,
        )
        scores.append(
            ServiceScore(
                facility_id=facility_id,
                coverage=round(coverage, 4),
                fairness=round(fairness, 4),
                accessibility=round(accessibility, 4),
                quality=round(quality, 4),
                seasonality=seasonality,
                overall=overall,
                tags=tuple(_service_tags(facts, facility_id, fairness, seasons)),
            )
        )
    return scores


def _service_tags(
    facts: Any,
    facility_id: str,
    fairness: float,
    seasons: Mapping[str, int],
) -> list[str]:
    tags: list[str] = []

    # 居民负担增加：居民使用占比相对基线下降（游客中心挤占休憩空间情形）
    for fact in _facts_for(facts, "USAGE_AGGREGATED"):
        d = _details(fact)
        if str(d.get("facility_id", "")) != facility_id:
            continue
        resident = d.get("resident_share")
        baseline = d.get("resident_share_baseline")
        if isinstance(resident, (int, float)) and isinstance(baseline, (int, float)):
            if resident + 0.1 < baseline:
                tags.append("resident_burden_increased")

    # 淡季低效闲置 vs 不可替代兜底：淡季利用率低时，
    # 若承担无障碍接驳等法定兜底职责则标记为 safety_net，否则标记闲置。
    peak = max(seasons.get("peak", 0), 1)
    if seasons.get("off", 0) / peak < 0.2:
        if _is_safety_net(facts, facility_id):
            tags.append("safety_net_low_utilization")
        else:
            tags.append("low_season_idle")

    # 待维护短板：存在未恢复故障或维护停用超期
    if _has_open_maintenance(facts, facility_id):
        tags.append("maintenance_gap")

    if fairness >= 0.8:
        tags.append("equitable_access")
    return sorted(set(tags))


def _is_safety_net(facts: Any, facility_id: str) -> bool:
    for fact in _facts_for(facts, "SERVICE_COMMITTED"):
        d = _details(fact)
        if str(d.get("facility_id", "")) != facility_id:
            continue
        if d.get("safety_net") is True or d.get("priority_group") in {
            "disabled",
            "elderly",
            "mobility_impaired",
        }:
            return True
    return False


def _has_open_maintenance(facts: Any, facility_id: str) -> bool:
    declared = {
        str(f.get("aggregate_id", ""))
        for f in _facts_for(facts, "OUTAGE_DECLARED")
        if facility_id in {str(s) for s in _details(f).get("all_service_ids", [])}
        or str(_details(f).get("facility_id", "")) == facility_id
    }
    resolved = {
        str(_details(f).get("outage_id", "")) for f in _facts_for(facts, "OUTAGE_RESOLVED")
    }
    return bool(declared - resolved)


def investment_journey_explanations(facts: Any) -> list[dict[str, Any]]:
    """把每笔公共投入关联到它改善的完整行程段落。

    SUBSIDY_PAID details：service_id、journey_stage（到达/游览/离开等）、
    beneficiary_groups、amount、source。输出只含群体规模与金额，
    不含任何个人标识，用于管理人员解释“这笔投入改善了谁的行程”。
    """
    explanations: list[dict[str, Any]] = []
    for fact in sorted(_facts_for(facts, "SUBSIDY_PAID"), key=lambda f: str(f.get("event_id", ""))):
        d = _details(fact)
        if d.get("source") not in PUBLIC_FUND_SOURCES:
            continue
        explanations.append(
            {
                "subsidy_id": str(fact.get("event_id", "")),
                "service_id": str(d.get("service_id", "")),
                "journey_stage": str(d.get("journey_stage", "")),
                "beneficiary_groups": tuple(str(g) for g in d.get("beneficiary_groups", [])),
                "amount_cents": int(d.get("amount", 0)),
                "source": str(d.get("source", "")),
            }
        )
    return explanations


def review_findings(facts: Any, *, k: int = DEFAULT_K_ANONYMITY) -> dict[str, Any]:
    """一次复盘的完整结论：问题清单、得分、资金分账与投入-行程解释。"""
    issues: list[LedgerIssue] = []
    for collector in (
        capacity_violations,
        fund_accounting_issues,
        outage_violations,
        evidence_scope_violations,
        decision_governance_violations,
    ):
        issues.extend(collector(facts))
    issues.extend(benefit_violations(facts))
    issues.extend(privacy_violations(facts, k=k))

    return {
        "issues": sorted(issues, key=lambda i: (i.rule, i.subject, i.code)),
        "fund_breakdown": breakdown_funds(facts),
        "service_scores": score_services(facts, k=k),
        "investments": investment_journey_explanations(facts),
    }
