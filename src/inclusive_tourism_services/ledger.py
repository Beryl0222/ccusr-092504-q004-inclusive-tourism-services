"""普惠旅游公共服务账本：事件流投影与领域规则。

设计原则
--------
* 只追加事件，投影状态由事件重放得到；评价必须在冻结版本上进行。
* 运营方只能提交本合同（operator_id 一致、service_codes 为合同子集）的履约证据。
* 公益清单容量不可被商业预约挤占；优惠按资格与生效期核验且跨渠道不重复。
* 财政拨款 / 政府购买服务 / 经营收入分账；故障只暂停受影响服务，恢复后不重复补贴。
* 居民使用与游客反馈只接受聚合数据，最小单元格以下拒收，禁止个人标识字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------

FUNDING_SOURCES = ("fiscal", "purchase_of_service", "operating_revenue")
BENEFIT_KINDS = ("voucher", "fee_waiver", "priority_service")
REVIEW_DIMENSIONS = ("coverage", "fairness", "accessibility", "quality", "seasonal_use")
# 聚合单元格小于该人数会暴露个人行踪，必须先并入更大组再入账。
MIN_CELL_COUNT = 5
# 个人标识/行踪字段禁止出现在使用与反馈事件中。
_FORBIDDEN_PERSONAL_KEYS = frozenset(
    {
        "person_id", "person_name", "id_card", "phone", "mobile",
        "user_id", "resident_id", "tourist_id", "name", "trace", "track",
    }
)


@dataclass(frozen=True)
class LedgerViolation:
    rule: str
    message: str
    event_id: str | None = None
    service_code: str | None = None


def _parse_date(value: Any) -> date | None:
    """容忍 date 与 date-time 字符串，只取日期部分。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 投影状态
# ---------------------------------------------------------------------------


@dataclass
class _Policy:
    code: str
    version: int
    supersedes: int | None = None
    effective_from: date | None = None
    public_services: set[str] = field(default_factory=set)


@dataclass
class _Contract:
    contract_id: str
    operator_id: str
    service_codes: set[str]
    period_start: date | None
    period_end: date | None


@dataclass
class _Benefit:
    code: str
    kind: str
    channels: frozenset[str]
    effective_from: date | None
    effective_to: date | None
    group: str


@dataclass
class _Outage:
    outage_id: str
    affected: frozenset[str]
    emergency: bool
    started_at: datetime | None = None
    resolved: bool = False
    resumed_at: datetime | None = None
    subsidy_paid: bool = False
    alternatives: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Register:
    """事件流的只读投影结果。"""

    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    facilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    contracts: dict[str, _Contract] = field(default_factory=dict)
    links: dict[str, dict[str, Any]] = field(default_factory=dict)
    benefits: dict[str, _Benefit] = field(default_factory=dict)
    policies: dict[str, _Policy] = field(default_factory=dict)
    outages: dict[str, _Outage] = field(default_factory=dict)
    funding: dict[tuple[str, str, str], float] = field(default_factory=dict)
    funding_events: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    freezes: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    applied_events: list[str] = field(default_factory=list)
    violations: list[LedgerViolation] = field(default_factory=list)
    redeemed_dedup_keys: set[str] = field(default_factory=set)

    # --- 读取辅助 ---------------------------------------------------------

    def is_public_listed(self, service_code: str) -> bool:
        service = self.services.get(service_code)
        return bool(service and service.get("public_benefit_listed"))

    def is_safety_net(self, service_code: str) -> bool:
        """兜底服务：列入公益清单且当前有活跃的无障碍衔接（接驳线等）。"""
        service = self.services.get(service_code)
        if not service or not service.get("public_benefit_listed"):
            return False
        return any(
            link.get("accessible")
            and link.get("active", True)
            and service_code in (link.get("service_codes") or [])
            for link in self.links.values()
        )

    def is_suspended(self, service_code: str) -> bool:
        """服务当前是否处于未恢复的停用/应急关闭中。"""
        return any(
            service_code in outage.affected and not outage.resolved
            for outage in self.outages.values()
        )

    def funding_totals(self) -> dict[str, float]:
        totals = {source: 0.0 for source in FUNDING_SOURCES}
        for (_period, source, _service), amount in self.funding.items():
            totals[source] = totals.get(source, 0.0) + amount
        return totals

    def funding_by_service(self, service_code: str) -> dict[str, float]:
        totals = {source: 0.0 for source in FUNDING_SOURCES}
        for (_period, source, svc), amount in self.funding.items():
            if svc == service_code:
                totals[source] += amount
        return totals

    def service_usage(self, service_code: str) -> list[dict[str, Any]]:
        return [row for row in self.usage if row["service_code"] == service_code]

    def service_feedback(self, service_code: str) -> list[dict[str, Any]]:
        return [row for row in self.feedback if row["service_code"] == service_code]

    def violations_for(self, service_code: str) -> list[LedgerViolation]:
        return [v for v in self.violations if v.service_code == service_code]

    # --- 判定输出 ---------------------------------------------------------

    def findings(self) -> list[dict[str, Any]]:
        """按服务汇记账本信号：挤占、闲置、待维护、兜底价值。"""
        result: list[dict[str, Any]] = []
        for code, service in sorted(self.services.items()):
            usage_rows = self.service_usage(code)
            low_season = [r for r in usage_rows if r.get("season") == "low"]
            low_load = None
            if low_season:
                saturations = [r["quota_saturation"] for r in low_season if r.get("quota_saturation") is not None]
                low_load = sum(saturations) / len(saturations) if saturations else None

            resident_share = None
            residents = sum(r.get("resident_visits", 0) for r in usage_rows)
            tourists = sum(r.get("tourist_visits", 0) for r in usage_rows)
            if residents + tourists:
                resident_share = residents / (residents + tourists)

            concerns = [
                row["concern"]
                for row in self.service_feedback(code)
                if row.get("concern")
            ]
            result.append(
                {
                    "service_code": code,
                    "service_name": service.get("service_name"),
                    "public_benefit_listed": bool(service.get("public_benefit_listed")),
                    "safety_net": self.is_safety_net(code),
                    "resident_share": resident_share,
                    "low_season_saturation": low_load,
                    "resident_space_displaced": service.get("resident_space_displaced", False),
                    "maintenance_backlog": service.get("maintenance_backlog", 0),
                    "suspended": self.is_suspended(code),
                    "funding_by_source": self.funding_by_service(code),
                    "top_concerns": sorted(set(concerns)),
                    "violations": [v.message for v in self.violations_for(code)],
                }
            )
        return result


# ---------------------------------------------------------------------------
# 事件重放
# ---------------------------------------------------------------------------


def replay(events: Iterable[Mapping[str, Any]]) -> Register:
    """按 occurred_at、event_id 稳定排序后重放，收集全部规则违反。

    事件携带信封字段（event_id/event_type/aggregate_id/occurred_at）与
    event_payload；重放器不做交换层校验（见 contracts.validate_event），
    只执行跨事件的领域规则。
    """
    ordered = sorted(events, key=lambda e: (str(e.get("occurred_at", "")), str(e.get("event_id", ""))))
    register = Register()

    for envelope in ordered:
        event_id = str(envelope.get("event_id", ""))
        event_type = envelope.get("event_type")
        body = envelope.get("event_payload") or {}
        if not isinstance(body, Mapping):
            body = {}
        handler = _HANDLERS.get(event_type)
        if handler is None:
            register.violations.append(
                LedgerViolation("unknown_event", f"未登记的事件类型 {event_type}", event_id)
            )
            continue
        handler(register, event_id, body, envelope)
        register.applied_events.append(event_id)

    _cross_check(register)
    return register


def _add(register: Register, violation: LedgerViolation) -> None:
    register.violations.append(violation)


# --- 政策与服务承诺 ---------------------------------------------------------


def _policy_published(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    code = p["policy_code"]
    version = int(p["policy_version"])
    existing = reg.policies.get(code)
    if existing is not None:
        _add(reg, LedgerViolation(
            "policy_version", f"政策 {code} 已发布，改版必须使用 POLICY_VERSIONED", eid))
        return
    reg.policies[code] = _Policy(
        code=code, version=version,
        effective_from=_parse_date(p.get("effective_from")),
    )


def _policy_versioned(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    code = p["policy_code"]
    version = int(p["policy_version"])
    supersedes = int(p["supersedes_version"])
    existing = reg.policies.get(code)
    if existing is None:
        _add(reg, LedgerViolation(
            "policy_version", f"政策 {code} 未经首发不能改版", eid))
        return
    if supersedes != existing.version:
        _add(reg, LedgerViolation(
            "policy_version",
            f"政策 {code} 第 {version} 版声明替代 {supersedes}，当前版本是 {existing.version}",
            eid))
        return
    if version <= supersedes:
        _add(reg, LedgerViolation(
            "policy_version", f"政策 {code} 新版本号必须高于被替代版本", eid))
        return
    if version != supersedes + 1:
        _add(reg, LedgerViolation(
            "policy_version",
            f"政策 {code} 第 {version} 版跳号：被替代版本为 {supersedes}，下一版必须是 {supersedes + 1}",
            eid))
        return
    existing.version = version
    existing.supersedes = supersedes
    existing.effective_from = _parse_date(p.get("effective_from"))


def _service_committed(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    code = p["service_code"]
    if code in reg.services:
        _add(reg, LedgerViolation(
            "service_commitment", f"服务 {code} 的开放承诺重复登记", eid, code))
        return
    reg.services[code] = {
        "service_code": code,
        "service_name": p.get("service_name"),
        "policy_code": p.get("policy_code"),
        "public_benefit_listed": bool(p.get("public_benefit_listed")),
        "service_radius_km": p.get("service_radius_km"),
        "opening_schedule": p.get("opening_schedule"),
        "operator_id": p.get("operator_id"),
        "resident_space_displaced": bool(p.get("resident_space_displaced", False)),
        "maintenance_backlog": 0,
    }
    policy = reg.policies.get(str(p.get("policy_code")))
    if policy is None:
        _add(reg, LedgerViolation(
            "policy_reference", f"服务 {code} 引用的政策版本不存在", eid, code))


# --- 设施能力 ---------------------------------------------------------------


def _capacity_reported(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    facility_id = p["facility_id"]
    total = _num(p.get("total_capacity"))
    public_quota = _num(p.get("public_quota"))
    commercial_quota = _num(p.get("commercial_quota"))
    bookings = _num(p.get("commercial_bookings"))

    if total is not None and public_quota is not None and commercial_quota is not None:
        if abs((public_quota + commercial_quota) - total) > 1e-9:
            _add(reg, LedgerViolation(
                "capacity_split",
                f"设施 {facility_id} 容量 {public_quota}+{commercial_quota} 不等于总容量 {total}",
                eid))
    if commercial_quota is not None and bookings is not None and bookings > commercial_quota + 1e-9:
        _add(reg, LedgerViolation(
            "public_quota_encroachment",
            f"设施 {facility_id} 商业预约 {bookings} 超过商业配额 {commercial_quota}，挤占公益容量",
            eid))
    reg.facilities[facility_id] = {
        "facility_id": facility_id,
        "total_capacity": total,
        "public_quota": public_quota,
        "commercial_quota": commercial_quota,
        "commercial_bookings": bookings,
        "as_of": p.get("as_of"),
    }


# --- 运营合同与履约证据 ------------------------------------------------------


def _contract_awarded(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    contract_id = p["contract_id"]
    services = set(p.get("service_codes") or [])
    unknown = sorted(services - set(reg.services))
    if unknown:
        _add(reg, LedgerViolation(
            "contract_scope", f"合同 {contract_id} 覆盖未承诺的服务 {unknown}", eid))
    start, end = _parse_date(p.get("period_start")), _parse_date(p.get("period_end"))
    if start and end and end < start:
        _add(reg, LedgerViolation(
            "contract_period", f"合同 {contract_id} 结束早于开始", eid))
    reg.contracts[contract_id] = _Contract(
        contract_id=contract_id,
        operator_id=p["operator_id"],
        service_codes=services,
        period_start=start,
        period_end=end,
    )


def _evidence_submitted(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    """运营方只能提交自身合同、且只覆盖合同范围内服务的履约证据。"""
    contract = reg.contracts.get(p.get("contract_id"))
    if contract is None:
        _add(reg, LedgerViolation(
            "evidence_scope", f"证据引用的合同 {p.get('contract_id')} 不存在", eid))
        return
    if p.get("operator_id") != contract.operator_id:
        _add(reg, LedgerViolation(
            "evidence_scope",
            f"运营方 {p.get('operator_id')} 不得提交合同 {contract.contract_id}"
            f"（归属 {contract.operator_id}）的履约证据",
            eid))
        return
    submitted = set(p.get("service_codes") or [])
    outside = sorted(submitted - contract.service_codes)
    if outside:
        _add(reg, LedgerViolation(
            "evidence_scope",
            f"运营方 {contract.operator_id} 只能提交本合同服务的证据，越界服务 {outside}",
            eid))
    submitted_on = _parse_date(p.get("submitted_on"))
    if submitted_on and contract.period_start and submitted_on < contract.period_start:
        _add(reg, LedgerViolation(
            "evidence_scope", "履约证据日期早于合同生效日", eid))
    if submitted_on and contract.period_end and submitted_on > contract.period_end:
        _add(reg, LedgerViolation(
            "evidence_scope", "履约证据日期晚于合同到期日", eid))


# --- 交通衔接 ---------------------------------------------------------------


def _link_opened(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    link_id = p["link_id"]
    services = list(p.get("service_codes") or [])
    unknown = sorted(set(services) - set(reg.services))
    if unknown:
        _add(reg, LedgerViolation(
            "link_reference", f"衔接线 {link_id} 挂接了未承诺的服务 {unknown}", eid))
    reg.links[link_id] = {
        "link_id": link_id,
        "mode": p.get("mode"),
        "service_codes": services,
        "accessible": bool(p.get("accessible")),
        "headway_minutes": p.get("headway_minutes"),
        "season": p.get("season"),
        "active": True,
    }


def _link_changed(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    link = reg.links.get(p.get("link_id"))
    if link is None:
        _add(reg, LedgerViolation(
            "link_reference", f"衔接线 {p.get('link_id')} 尚未开通即变更", eid))
        return
    kind = p.get("change_kind")
    if kind == "suspended" or kind == "withdrawn":
        link["active"] = False
    elif kind == "restored":
        link["active"] = True
    for key in ("mode", "headway_minutes", "season", "accessible"):
        if key in p and p[key] is not None:
            link[key] = p[key]


# --- 优惠资格、生效期与去重 ---------------------------------------------------


def _benefit_granted(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    code = p["benefit_code"]
    if code in reg.benefits:
        _add(reg, LedgerViolation(
            "benefit_catalog", f"优惠 {code} 重复登记", eid))
        return
    kind = p.get("benefit_kind")
    if kind not in BENEFIT_KINDS:
        _add(reg, LedgerViolation(
            "benefit_catalog", f"优惠类型 {kind} 未登记（券/减免/优先服务）", eid))
    if p.get("budget_source") not in FUNDING_SOURCES:
        _add(reg, LedgerViolation(
            "funding_source", f"优惠 {code} 的资金来源 {p.get('budget_source')} 未分账登记", eid))
    rule = p.get("qualification_rule")
    if not isinstance(rule, Mapping) or "eligible_group" not in rule:
        _add(reg, LedgerViolation(
            "qualification", f"优惠 {code} 必须声明资格规则 eligible_group", eid))
    channels = set(p.get("channels") or [])
    channels.add(str(p.get("channel")))
    reg.benefits[code] = _Benefit(
        code=code,
        kind=str(kind),
        channels=frozenset(c for c in channels if c),
        effective_from=_parse_date(p.get("effective_from")),
        effective_to=_parse_date(p.get("effective_to")),
        group=str(rule.get("eligible_group", "")) if isinstance(rule, Mapping) else "",
    )


def _benefit_redeemed(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    code = p.get("benefit_code")
    benefit = reg.benefits.get(code)
    on = _parse_date(p.get("occurred_on"))
    channel = p.get("channel")

    if benefit is None:
        _add(reg, LedgerViolation("benefit_redemption", f"优惠 {code} 未登记即核销", eid))
    else:
        if on is not None and benefit.effective_from and on < benefit.effective_from:
            _add(reg, LedgerViolation(
                "benefit_window", f"优惠 {code} 核销日 {on} 早于生效日 {benefit.effective_from}", eid))
        if on is not None and benefit.effective_to and on > benefit.effective_to:
            _add(reg, LedgerViolation(
                "benefit_window", f"优惠 {code} 核销日 {on} 晚于失效日 {benefit.effective_to}", eid))
        if channel and benefit.channels and channel not in benefit.channels:
            _add(reg, LedgerViolation(
                "benefit_channel",
                f"优惠 {code} 在未登记渠道 {channel} 核销（登记渠道：{sorted(benefit.channels)}）",
                eid))
    if p.get("qualification_met") is not True:
        _add(reg, LedgerViolation(
            "qualification", f"核销 {p.get('redemption_id')} 资格核验未通过", eid))

    dedup = p.get("dedup_key")
    if not dedup:
        _add(reg, LedgerViolation(
            "benefit_dedup", f"核销 {p.get('redemption_id')} 缺少去重键", eid))
    elif dedup in reg.redeemed_dedup_keys:
        _add(reg, LedgerViolation(
            "benefit_dedup", f"同一受益 {dedup} 跨渠道重复计算", eid))
    else:
        reg.redeemed_dedup_keys.add(str(dedup))

    if p.get("service_code") not in reg.services:
        _add(reg, LedgerViolation(
            "service_reference", f"核销挂接的服务 {p.get('service_code')} 不存在", eid))


# --- 资金分账 ---------------------------------------------------------------


def _funding_allocated(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    _apply_funding(reg, eid, p, sign=1)


def _funding_settled(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    _apply_funding(reg, eid, p, sign=1, settled=True)


def _apply_funding(reg: Register, eid: str, p: Mapping[str, Any], *, sign: int, settled: bool = False) -> None:
    source = p.get("source")
    if source not in FUNDING_SOURCES:
        _add(reg, LedgerViolation(
            "funding_source", f"资金来源 {source} 不在财政/购买服务/经营收入分账内", eid))
        return
    service_code = p.get("service_code")
    if service_code not in reg.services:
        _add(reg, LedgerViolation(
            "service_reference", f"资金挂接的服务 {service_code} 不存在", eid, service_code))
    amount = _num(p.get("amount"))
    if amount is None or amount < 0:
        _add(reg, LedgerViolation("funding_amount", "资金金额必须是非负数", eid))
        return
    period = str(p.get("fiscal_period"))
    key = (period, source, service_code)
    reg.funding[key] = reg.funding.get(key, 0.0) + sign * amount
    record = {
        "event_id": eid,
        "funding_id": p.get("funding_id"),
        "fiscal_period": period,
        "source": source,
        "amount": sign * amount,
        "service_code": service_code,
        "settled": settled,
        "outage_id": p.get("outage_id"),
    }
    if settled:
        record["settled_on"] = p.get("settled_on")
    reg.funding_events.append(record)


# --- 停用、替代与恢复 --------------------------------------------------------


def _outage_declared(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    outage_id = p["outage_id"]
    if outage_id in reg.outages:
        _add(reg, LedgerViolation("outage", f"停用单 {outage_id} 重复申报", eid))
        return
    affected = frozenset(p.get("affected_service_codes") or [])
    unknown = sorted(affected - set(reg.services))
    if unknown:
        _add(reg, LedgerViolation(
            "service_reference", f"停用单挂接了不存在的服务 {unknown}", eid))
    started = _parse_dt(p.get("started_at")) or _parse_dt(env.get("occurred_at"))
    # 只暂停受影响服务：登记进停用集合，未列出的服务保持运行（投影自动体现）。
    reg.outages[outage_id] = _Outage(
        outage_id=outage_id,
        affected=affected,
        emergency=bool(p.get("emergency")),
        started_at=started,
    )


def _alternative_arranged(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    outage = reg.outages.get(p.get("outage_id"))
    if outage is None:
        _add(reg, LedgerViolation(
            "outage", f"替代安排引用的停用单 {p.get('outage_id')} 不存在", eid))
        return
    replacement = p.get("replacement_link_id")
    if replacement is not None and replacement not in reg.links:
        _add(reg, LedgerViolation(
            "link_reference", f"替代衔接线 {replacement} 未登记", eid))
    covered = frozenset(p.get("affected_service_codes") or outage.affected)
    missing = sorted(outage.affected - covered)
    if missing:
        _add(reg, LedgerViolation(
            "alternative_coverage", f"停用服务 {missing} 没有替代安排兜底", eid))
    outage.alternatives.append({
        "replacement_link_id": replacement,
        "covered": covered,
        "started_at": p.get("started_at"),
    })


def _service_resumed(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    outage = reg.outages.get(p.get("outage_id"))
    if outage is None:
        _add(reg, LedgerViolation(
            "outage", f"恢复引用的停用单 {p.get('outage_id')} 不存在", eid))
        return
    restored = frozenset(p.get("restored_service_codes") or [])
    over = sorted(restored - outage.affected)
    if over:
        _add(reg, LedgerViolation(
            "outage_scope", f"恢复操作超出停用单范围 {over}", eid))
    if outage.resolved:
        _add(reg, LedgerViolation("outage", f"停用单 {outage.outage_id} 已恢复，不能重复恢复", eid))
    outage.resolved = True
    outage.resumed_at = _parse_dt(p.get("resumed_at"))
    if not outage.alternatives:
        _add(reg, LedgerViolation(
            "alternative_coverage", f"停用单 {outage.outage_id} 关闭期间没有替代安排", eid))


def _maintenance_logged(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    facility_id = p.get("facility_id")
    if facility_id not in reg.facilities:
        _add(reg, LedgerViolation(
            "facility_reference", f"维护记录引用的设施 {facility_id} 未登记", eid))
    facility = reg.facilities.setdefault(facility_id, {"facility_id": facility_id})
    status = p.get("status")
    if status in ("backlog", "overdue"):
        # 待维护短板先记在设施上，再归集到其承载的公益服务。
        facility["maintenance_backlog"] = facility.get("maintenance_backlog", 0) + 1
        for code in p.get("service_codes") or []:
            service = reg.services.get(code)
            if service is None:
                _add(reg, LedgerViolation(
                    "service_reference", f"维护记录挂接的服务 {code} 不存在", eid, code))
                continue
            service["maintenance_backlog"] = service.get("maintenance_backlog", 0) + 1


# --- 使用与反馈（聚合脱敏）---------------------------------------------------


def _usage_recorded(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    _reject_personal_keys(reg, eid, p)
    service_code = p.get("service_code")
    if service_code not in reg.services:
        _add(reg, LedgerViolation(
            "service_reference", f"使用记录的服务 {service_code} 不存在", eid, service_code))
    resident, tourist = _num(p.get("resident_visits")) or 0, _num(p.get("tourist_visits")) or 0
    period = p.get("period_id")
    if _period_frozen(reg, period):
        _add(reg, LedgerViolation(
            "frozen_version", f"评价期 {period} 已冻结，使用数据不可再写入", eid, service_code))
        return
    saturation = p.get("quota_saturation")
    if saturation is not None and not _in_range(saturation, 0, 1):
        _add(reg, LedgerViolation(
            "usage_value", "配额饱和度必须在 0~1 之间", eid, service_code))
    if resident < 0 or tourist < 0:
        _add(reg, LedgerViolation("usage_value", "人次不能为负", eid, service_code))
    reg.usage.append({
        "period_id": period,
        "service_code": service_code,
        "season": p.get("season"),
        "resident_visits": resident,
        "tourist_visits": tourist,
        "quota_saturation": _num(saturation),
    })


def _feedback_collected(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    _reject_personal_keys(reg, eid, p)
    service_code = p.get("service_code")
    if service_code not in reg.services:
        _add(reg, LedgerViolation(
            "service_reference", f"反馈记录的服务 {service_code} 不存在", eid, service_code))
    cell_count = p.get("cell_count")
    if not isinstance(cell_count, int) or isinstance(cell_count, bool) or cell_count < MIN_CELL_COUNT:
        _add(reg, LedgerViolation(
            "privacy_cell",
            f"反馈单元格 {cell_count} 人小于最小匿名单元格 {MIN_CELL_COUNT} 人，不得入账",
            eid, service_code))
        return
    period = p.get("period_id")
    if _period_frozen(reg, period):
        _add(reg, LedgerViolation(
            "frozen_version", f"评价期 {period} 已冻结，反馈不可再写入", eid, service_code))
        return
    satisfaction = p.get("satisfaction")
    if satisfaction is not None and not _in_range(satisfaction, 0, 1):
        _add(reg, LedgerViolation(
            "feedback_value", "满意度必须在 0~1 之间", eid, service_code))
    reg.feedback.append({
        "period_id": period,
        "service_code": service_code,
        "group": p.get("group"),
        "satisfaction": _num(satisfaction),
        "concern": p.get("concern"),
        "cell_count": cell_count,
    })


def _reject_personal_keys(reg: Register, eid: str, p: Mapping[str, Any]) -> None:
    leaked = sorted(key for key in p if str(key).lower() in _FORBIDDEN_PERSONAL_KEYS)
    if leaked:
        _add(reg, LedgerViolation(
            "privacy_personal", f"禁止携带个人标识/行踪字段 {leaked}，只接受聚合数据", eid))


# --- 冻结、回避与评价决定 ----------------------------------------------------


def _period_frozen(reg: Register, period: Any) -> bool:
    return period is not None and period in reg.freezes


def _performance_frozen(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    period = p["period_id"]
    if period in reg.freezes:
        _add(reg, LedgerViolation(
            "frozen_version", f"评价期 {period} 不得重复冻结", eid))
        return
    if not p.get("dataset_hash"):
        _add(reg, LedgerViolation("frozen_version", "冻结必须记录数据版本哈希", eid))
    reg.freezes[period] = {
        "period_id": period,
        "frozen_at": p.get("frozen_at"),
        "event_id_range": p.get("event_id_range"),
        "dataset_hash": p.get("dataset_hash"),
    }


def _reviewer_assigned(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    period = p["period_id"]
    if not p.get("conflict_check"):
        _add(reg, LedgerViolation(
            "conflict_of_interest", f"评价期 {period} 未完成利益相关者回避核查", eid))
    reviewers = list(p.get("reviewer_ids") or [])
    # reviewer 不得是在营运营方。
    operators = {c.operator_id for c in reg.contracts.values()}
    conflicted = sorted(set(reviewers) & operators)
    if conflicted:
        _add(reg, LedgerViolation(
            "conflict_of_interest", f"评价人员 {conflicted} 与运营方存在利益关系，必须回避", eid))
    reg.review_assignments[period] = {
        "reviewer_ids": reviewers,
        "conflict_check": bool(p.get("conflict_check")),
    }


def _review_decided(reg: Register, eid: str, p: Mapping[str, Any], env: Mapping[str, Any]) -> None:
    period = p["period_id"]
    freeze = reg.freezes.get(period)
    if freeze is None:
        _add(reg, LedgerViolation(
            "frozen_version", f"评价期 {period} 未冻结数据版本，不得作出决定", eid))
    elif p.get("frozen_dataset_hash") != freeze["dataset_hash"]:
        _add(reg, LedgerViolation(
            "frozen_version",
            "决定引用的数据哈希与冻结版本不一致，必须基于冻结数据综合评价",
            eid))
    assignment = reg.review_assignments.get(period)
    if assignment is None or not assignment.get("conflict_check"):
        _add(reg, LedgerViolation(
            "conflict_of_interest", f"评价期 {period} 未完成回避核查", eid))

    dimensions = p.get("dimensions") or {}
    missing = [d for d in REVIEW_DIMENSIONS if d not in dimensions]
    if missing:
        _add(reg, LedgerViolation(
            "review_dimensions", f"评价缺少维度 {missing}（覆盖/公平/可达/质量/淡旺季使用）", eid))
    for name, score in dimensions.items():
        if name not in REVIEW_DIMENSIONS or not _in_range(score, 0, 1):
            _add(reg, LedgerViolation(
                "review_dimensions", f"评价维度 {name} 或取值 {score} 非法", eid))

    decision = p.get("decision")
    if decision not in ("continue", "rectify", "withdraw"):
        _add(reg, LedgerViolation(
            "review_decision", f"决定 {decision} 必须是 continue/rectify/withdraw", eid))

    record = {
        "period_id": period,
        "decision_id": p.get("decision_id"),
        "decision": decision,
        "dimensions": dimensions,
        "service_codes": list(p.get("service_codes") or reg.services.keys()),
        "rationale": p.get("rationale", ""),
    }
    reg.decisions.append(record)

    # 兜底服务保护：列入公益清单且不可替代的服务，淡季低利用率不能成为撤线理由。
    if decision == "withdraw":
        for code in record["service_codes"]:
            if reg.is_safety_net(code):
                rows = reg.service_usage(code)
                low = [r for r in rows if r.get("season") == "low"]
                if low and not _has_alternative_for(reg, code):
                    _add(reg, LedgerViolation(
                        "safety_net",
                        f"兜底服务 {code} 淡季低利用但无替代衔接，不得仅按客流撤线",
                        eid, code))


def _has_alternative_for(reg: Register, service_code: str) -> bool:
    """该公益服务是否还有另一条活跃的无障碍衔接可承担兜底。"""
    count = 0
    for link in reg.links.values():
        if link.get("accessible") and link.get("active") and service_code in (link.get("service_codes") or []):
            count += 1
    return count >= 2


# --- 跨事件核查 --------------------------------------------------------------


def _cross_check(reg: Register) -> None:
    _check_outage_subsidies(reg)
    _check_resident_space(reg)


def _check_outage_subsidies(reg: Register) -> None:
    """停运补偿按停用单核销：同一停用单最多一次，恢复之后再拨即重复补贴。"""
    paid_outages: set[str] = set()
    for record in reg.funding_events:
        outage_id = record.get("outage_id")
        eid = record.get("event_id")
        if not outage_id:
            continue
        outage = reg.outages.get(outage_id)
        if outage is None:
            _add(reg, LedgerViolation(
                "duplicate_subsidy",
                f"停运补偿挂接的停用单 {outage_id} 不存在",
                event_id=eid,
                service_code=record["service_code"],
            ))
            continue
        if outage_id in paid_outages:
            _add(reg, LedgerViolation(
                "duplicate_subsidy",
                f"停用单 {outage_id} 已补偿一次，恢复后不得重复补贴",
                event_id=eid,
                service_code=record["service_code"],
            ))
            continue
        paid_outages.add(outage_id)
        settled_on = _parse_date(record.get("settled_on"))
        if outage.resolved and outage.resumed_at is not None and settled_on is not None \
                and settled_on >= outage.resumed_at.date():
            _add(reg, LedgerViolation(
                "duplicate_subsidy",
                f"停用单 {outage_id} 已于 {outage.resumed_at.date()} 恢复，之后不得再领取停运补偿",
                event_id=eid,
                service_code=record["service_code"],
            ))


def _check_resident_space(reg: Register) -> None:
    """居民休憩空间被挤占必须在账本留痕（游客中心高客流场景）。"""
    for code, service in reg.services.items():
        if service.get("resident_space_displaced"):
            displaced_concern = any(
                row["service_code"] == code and "居民" in str(row.get("concern", ""))
                for row in reg.feedback
            )
            if not displaced_concern:
                _add(reg, LedgerViolation(
                    "resident_burden",
                    f"服务 {code} 已标记挤占居民休憩空间，但居民反馈中没有对应负担记录",
                    service_code=code,
                ))


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------


def explain_investment(reg: Register, service_code: str) -> dict[str, Any]:
    """解释一笔投入改善了谁的完整行程：资金 → 服务 → 覆盖人口与使用。"""
    service = reg.services.get(service_code)
    if service is None:
        return {"service_code": service_code, "known": False}
    usage_rows = reg.service_usage(service_code)
    residents = sum(r["resident_visits"] for r in usage_rows)
    tourists = sum(r["tourist_visits"] for r in usage_rows)
    links = [
        link["link_id"] for link in reg.links.values()
        if link.get("active", True) and service_code in (link.get("service_codes") or [])
    ]
    return {
        "service_code": service_code,
        "known": True,
        "service_name": service.get("service_name"),
        "funding_by_source": reg.funding_by_service(service_code),
        "service_radius_km": service.get("service_radius_km"),
        "accessible_links": links,
        "resident_visits": residents,
        "tourist_visits": tourists,
        "public_benefit_listed": service.get("public_benefit_listed"),
    }


def low_utilization_services(reg: Register, *, threshold: float = 0.2) -> list[dict[str, Any]]:
    """淡季低利用率清单，并标注是否承担不可替代的兜底服务。"""
    result = []
    for finding in reg.findings():
        load = finding["low_season_saturation"]
        if load is not None and load < threshold:
            result.append({
                "service_code": finding["service_code"],
                "low_season_saturation": load,
                "safety_net": finding["safety_net"],
                "recommendation": (
                    "保留兜底服务，核查替代安排后再议班次"
                    if finding["safety_net"]
                    else "复核公益必要性与开放承诺"
                ),
            })
    return result


def maintenance_backlog(reg: Register) -> dict[str, list[str]]:
    """待维护短板：按设施列出，并映射到承载的公益服务。"""
    facilities = [
        facility_id for facility_id, item in reg.facilities.items()
        if item.get("maintenance_backlog", 0) > 0
    ]
    services = [
        code for code, service in reg.services.items()
        if service.get("maintenance_backlog", 0) > 0
    ]
    return {"facilities": sorted(facilities), "services": sorted(services)}


# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _in_range(value: Any, low: float, high: float) -> bool:
    number = _num(value)
    return number is not None and low <= number <= high


_HANDLERS = {
    "POLICY_PUBLISHED": _policy_published,
    "POLICY_VERSIONED": _policy_versioned,
    "SERVICE_COMMITTED": _service_committed,
    "CAPACITY_REPORTED": _capacity_reported,
    "CONTRACT_AWARDED": _contract_awarded,
    "LINK_OPENED": _link_opened,
    "LINK_CHANGED": _link_changed,
    "BENEFIT_GRANTED": _benefit_granted,
    "BENEFIT_REDEEMED": _benefit_redeemed,
    "FUNDING_ALLOCATED": _funding_allocated,
    "FUNDING_SETTLED": _funding_settled,
    "OUTAGE_DECLARED": _outage_declared,
    "ALTERNATIVE_ARRANGED": _alternative_arranged,
    "SERVICE_RESUMED": _service_resumed,
    "MAINTENANCE_LOGGED": _maintenance_logged,
    "USAGE_RECORDED": _usage_recorded,
    "FEEDBACK_COLLECTED": _feedback_collected,
    "EVIDENCE_SUBMITTED": _evidence_submitted,
    "PERFORMANCE_FROZEN": _performance_frozen,
    "REVIEWER_ASSIGNED": _reviewer_assigned,
    "REVIEW_DECIDED": _review_decided,
}
