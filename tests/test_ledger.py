from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inclusive_tourism_services.ledger import (
    MIN_CELL_COUNT,
    explain_investment,
    low_utilization_services,
    maintenance_backlog,
    replay,
)


SAMPLE = json.loads((ROOT / "data" / "sample_ledger.json").read_text(encoding="utf-8"))
FREEZE_HASH = "sha256:9f2c1a7e4b8d6f30a1c2e5b7d8904f61a3c2e5b7d8904f61a3c2e5b7d8904f61"


def event(event_type, aggregate_type, aggregate_id, payload, eid, at="2026-06-01T10:00:00+08:00", version=1):
    return {
        "event_id": eid,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": at,
        "version": version,
        "summary": eid,
        "event_payload": payload,
    }


def policy_published(eid="T-001", version=1, at="2026-01-01T09:00:00+08:00"):
    return event(
        "POLICY_PUBLISHED", "service_policy", "policy-P",
        {"policy_code": "P", "policy_version": version, "effective_from": "2026-01-01", "issuer": "局"},
        eid, at,
    )


def service_committed(code="SVC-1", eid="T-002", public=True, at="2026-01-02T09:00:00+08:00", **extra):
    payload = {
        "policy_code": "P",
        "service_code": code,
        "service_name": code,
        "public_benefit_listed": public,
        "service_radius_km": 2.0,
        "opening_schedule": "全年",
        "operator_id": "OP-1",
    }
    payload.update(extra)
    return event("SERVICE_COMMITTED", "service_policy", f"service-{code}", payload, eid, at)


def contract(contract_id="C-1", operator="OP-1", services=("SVC-1",), eid="T-003"):
    return event(
        "CONTRACT_AWARDED", "operating_contract", f"contract-{contract_id}",
        {
            "contract_id": contract_id,
            "operator_id": operator,
            "service_codes": list(services),
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
            "purchase_amount": 100,
        },
        eid,
    )


def codes(reg, rule=None):
    if rule is None:
        return {v.rule for v in reg.violations}
    return {v.event_id for v in reg.violations if v.rule == rule}


class SampleLedgerTests(unittest.TestCase):
    def setUp(self):
        self.reg = replay(copy.deepcopy(SAMPLE))

    def test_only_three_deliberate_violations(self):
        by_rule = {}
        for v in self.reg.violations:
            by_rule.setdefault(v.rule, []).append(v.event_id)
        self.assertEqual(
            by_rule,
            {
                "public_quota_encroachment": ["2026-0007"],
                "benefit_dedup": ["2026-0015"],
                "frozen_version": ["2026-0036"],
            },
        )

    def test_funding_is_split_by_three_sources(self):
        totals = self.reg.funding_totals()
        self.assertEqual(totals["fiscal"], 1_200_000)
        self.assertEqual(totals["purchase_of_service"], 432_000)
        self.assertEqual(totals["operating_revenue"], 376_500)

    def test_decisions_match_review_findings(self):
        decisions = {d["service_codes"][0]: d["decision"] for d in self.reg.decisions}
        self.assertEqual(decisions["SVC-VC-01"], "rectify")
        self.assertEqual(decisions["SVC-SHUTTLE-B1"], "continue")

    def test_shuttle_is_safety_net_low_utilization_but_kept(self):
        low = {item["service_code"]: item for item in low_utilization_services(self.reg)}
        self.assertIn("SVC-SHUTTLE-B1", low)
        self.assertTrue(low["SVC-SHUTTLE-B1"]["safety_net"])
        self.assertNotIn("SVC-VC-01", low)

    def test_visitor_center_signals(self):
        finding = {f["service_code"]: f for f in self.reg.findings()}["SVC-VC-01"]
        self.assertTrue(finding["resident_space_displaced"])
        self.assertEqual(finding["maintenance_backlog"], 1)
        self.assertFalse(finding["safety_net"])

    def test_maintenance_backlog_view(self):
        self.assertEqual(
            maintenance_backlog(self.reg),
            {"facilities": ["FAC-VC-01"], "services": ["SVC-VC-01"]},
        )

    def test_investment_explanation_links_money_to_trips(self):
        vc = explain_investment(self.reg, "SVC-VC-01")
        self.assertEqual(vc["funding_by_source"]["fiscal"], 1_200_000)
        self.assertEqual(vc["resident_visits"], 5700)
        self.assertEqual(vc["tourist_visits"], 42700)
        # 临时替代出租车已退出，不计入常态衔接。
        b1 = explain_investment(self.reg, "SVC-SHUTTLE-B1")
        self.assertEqual(b1["accessible_links"], ["LINK-B1"])


class CapacityTests(unittest.TestCase):
    def test_capacity_split_must_equal_total(self):
        events = [
            policy_published(),
            service_committed(),
            event(
                "CAPACITY_REPORTED", "public_facility", "F-1",
                {"facility_id": "F-1", "total_capacity": 10, "public_quota": 5,
                 "commercial_quota": 4, "commercial_bookings": 0, "as_of": "2026-02-01"},
                "C-1",
            ),
        ]
        self.assertEqual(codes(replay(events)), {"capacity_split"})

    def test_commercial_bookings_cannot_encroach_public_quota(self):
        events = [
            policy_published(),
            service_committed(),
            event(
                "CAPACITY_REPORTED", "public_facility", "F-1",
                {"facility_id": "F-1", "total_capacity": 10, "public_quota": 6,
                 "commercial_quota": 4, "commercial_bookings": 5, "as_of": "2026-02-01"},
                "C-1",
            ),
        ]
        self.assertEqual(codes(replay(events)), {"public_quota_encroachment"})


class BenefitTests(unittest.TestCase):
    def _grant(self, eid="B-1", **overrides):
        payload = {
            "benefit_code": "BEN-1",
            "benefit_kind": "voucher",
            "channel": "online",
            "channels": ["online", "offline_window"],
            "qualification_rule": {"eligible_group": "游客"},
            "effective_from": "2026-07-01",
            "effective_to": "2026-07-31",
            "budget_source": "fiscal",
        }
        payload.update(overrides)
        return event("BENEFIT_GRANTED", "benefit_entitlement", "benefit-BEN-1", payload, eid)

    def _redeem(self, eid, **overrides):
        payload = {
            "benefit_code": "BEN-1",
            "redemption_id": f"RD-{eid}",
            "channel": "online",
            "qualification_met": True,
            "occurred_on": "2026-07-10",
            "service_code": "SVC-1",
            "dedup_key": f"key-{eid}",
        }
        payload.update(overrides)
        return event("BENEFIT_REDEEMED", "benefit_entitlement", "benefit-BEN-1", payload, eid)

    def test_window_qualification_channel_and_dedup(self):
        base = [policy_published(), service_committed(), self._grant()]

        cases = {
            "R-early": self._redeem("R-early", occurred_on="2026-06-30"),
            "R-late": self._redeem("R-late", occurred_on="2026-08-01"),
            "R-noqual": self._redeem("R-noqual", qualification_met=False),
            "R-badch": self._redeem("R-badch", channel="street_hawker"),
            "R-nodedup": self._redeem("R-nodedup", dedup_key=""),
            "R-dup1": self._redeem("R-dup1", dedup_key="same"),
            "R-dup2": self._redeem("R-dup2", dedup_key="same"),
        }
        expected = {
            "R-early": "benefit_window",
            "R-late": "benefit_window",
            "R-noqual": "qualification",
            "R-badch": "benefit_channel",
            "R-nodedup": "benefit_dedup",
        }
        for eid, want in expected.items():
            reg = replay(base + [cases[eid]])
            self.assertIn(want, codes(reg), eid)

        reg = replay(base + [cases["R-dup1"], cases["R-dup2"]])
        self.assertEqual(codes(reg, "benefit_dedup"), {"R-dup2"})

    def test_cross_channel_redemption_with_unique_dedup_is_allowed(self):
        events = [
            policy_published(), service_committed(), self._grant(),
            self._redeem("R-1", channel="online", dedup_key="k1"),
            self._redeem("R-2", channel="offline_window", dedup_key="k2"),
        ]
        self.assertEqual(codes(replay(events)), set())

    def test_grant_requires_qualification_rule_and_known_source(self):
        events = [
            policy_published(), service_committed(),
            self._grant("B-bad", qualification_rule={}, budget_source="sponsorship_undeclared"),
        ]
        self.assertEqual(codes(replay(events)), {"qualification", "funding_source"})


class FundingTests(unittest.TestCase):
    def test_unknown_source_service_and_negative_amount(self):
        events = [
            policy_published(), service_committed(),
            event(
                "FUNDING_ALLOCATED", "funding_ledger", "F-1",
                {"funding_id": "F1", "fiscal_period": "2026", "source": "sponsorship",
                 "amount": 100, "service_code": "SVC-1"},
                "FN-1",
            ),
            event(
                "FUNDING_ALLOCATED", "funding_ledger", "F-2",
                {"funding_id": "F2", "fiscal_period": "2026", "source": "fiscal",
                 "amount": -1, "service_code": "SVC-404"},
                "FN-2",
            ),
        ]
        reg = replay(events)
        self.assertIn("funding_source", codes(reg))
        self.assertIn("funding_amount", codes(reg))
        self.assertIn("service_reference", codes(reg))


class EvidenceTests(unittest.TestCase):
    def _evidence(self, eid, contract_id="C-1", operator="OP-1", services=("SVC-1",),
                  submitted_on="2026-06-01", at="2026-06-02T10:00:00+08:00"):
        return event(
            "EVIDENCE_SUBMITTED", "operating_contract", f"contract-{contract_id}",
            {"contract_id": contract_id, "operator_id": operator,
             "service_codes": list(services), "evidence_ids": ["E1"],
             "submitted_on": submitted_on},
            eid, at,
        )

    def test_operator_can_only_submit_own_contract_scope(self):
        base = [policy_published(), service_committed(), contract()]
        reg = replay(base + [self._evidence("E-other", operator="OP-INTRUDER")])
        self.assertEqual(codes(reg), {"evidence_scope"})

        reg = replay(base + [self._evidence("E-scope", services=("SVC-9",))])
        self.assertEqual(codes(reg), {"evidence_scope"})

        reg = replay(base + [self._evidence("E-unknown", contract_id="C-404")])
        self.assertEqual(codes(reg), {"evidence_scope"})

        reg = replay(base + [self._evidence("E-ok")])
        self.assertEqual(codes(reg), set())

    def test_evidence_must_fall_within_contract_period(self):
        base = [policy_published(), service_committed(), contract()]
        reg = replay(base + [self._evidence("E-before", submitted_on="2025-12-31")])
        self.assertIn("evidence_scope", codes(reg))


class PolicyVersionTests(unittest.TestCase):
    def test_version_chain_must_be_contiguous(self):
        reg = replay([
            policy_published(),
            event(
                "POLICY_VERSIONED", "service_policy", "policy-P",
                {"policy_code": "P", "policy_version": 3, "supersedes_version": 1,
                 "effective_from": "2026-03-01"},
                "V-jump",
            ),
        ])
        self.assertEqual(codes(reg), {"policy_version"})

        reg = replay([
            event(
                "POLICY_VERSIONED", "service_policy", "policy-P",
                {"policy_code": "P", "policy_version": 2, "supersedes_version": 1,
                 "effective_from": "2026-03-01"},
                "V-first",
            ),
        ])
        self.assertEqual(codes(reg), {"policy_version"})

    def test_valid_version_chain(self):
        reg = replay([
            policy_published(),
            event(
                "POLICY_VERSIONED", "service_policy", "policy-P",
                {"policy_code": "P", "policy_version": 2, "supersedes_version": 1,
                 "effective_from": "2026-03-01"},
                "V-ok",
            ),
        ])
        self.assertEqual(codes(reg), set())


class OutageTests(unittest.TestCase):
    def _outage(self, eid="O-1", services=("SVC-1",), at="2026-08-01T10:00:00+08:00"):
        return event(
            "OUTAGE_DECLARED", "transit_link", "L-1",
            {"outage_id": "OUT-1", "affected_service_codes": list(services),
             "reason": "检修", "started_at": at, "emergency": False},
            eid, at,
        )

    def _alternative(self, eid="O-2", link="L-ALT"):
        return event(
            "LINK_OPENED", "transit_link", link,
            {"link_id": link, "mode": "接驳", "service_codes": ["SVC-1"],
             "accessible": True, "headway_minutes": 20, "season": "all"},
            "L-open", "2026-07-01T08:00:00+08:00",
        ), event(
            "ALTERNATIVE_ARRANGED", "transit_link", "L-1",
            {"outage_id": "OUT-1", "affected_service_codes": ["SVC-1"],
             "replacement_link_id": link, "started_at": "2026-08-01T10:30:00+08:00"},
            eid, "2026-08-01T10:30:00+08:00",
        )

    def _resume(self, eid="O-3", at="2026-08-03T08:00:00+08:00", services=("SVC-1",)):
        return event(
            "SERVICE_RESUMED", "transit_link", "L-1",
            {"outage_id": "OUT-1", "resumed_at": at,
             "restored_service_codes": list(services)},
            eid, at,
        )

    def test_only_affected_service_is_suspended(self):
        events = [
            policy_published(),
            service_committed("SVC-1"),
            service_committed("SVC-2", "T-202"),
            self._outage(services=("SVC-1",)),
        ]
        reg = replay(events)
        self.assertTrue(reg.is_suspended("SVC-1"))
        self.assertFalse(reg.is_suspended("SVC-2"))

    def test_resume_without_alternative_is_violation(self):
        reg = replay([
            policy_published(), service_committed(),
            self._outage(), self._resume(),
        ])
        self.assertIn("alternative_coverage", codes(reg))

    def test_resume_cannot_exceed_outage_scope(self):
        opened, alt = self._alternative()
        reg = replay([
            policy_published(), service_committed("SVC-1"), service_committed("SVC-2", "T2"),
            self._outage(services=("SVC-1",)), opened, alt,
            self._resume(services=("SVC-1", "SVC-2")),
        ])
        self.assertIn("outage_scope", codes(reg))

    def _subsidy(self, eid, settled_on, amount=1000):
        return event(
            "FUNDING_SETTLED", "funding_ledger", "F-OUT",
            {"funding_id": eid, "fiscal_period": "2026Q3",
             "source": "purchase_of_service", "amount": amount,
             "service_code": "SVC-1", "settled_on": settled_on, "outage_id": "OUT-1"},
            eid, f"{settled_on}T12:00:00+08:00",
        )

    def test_subsidy_once_and_not_after_resume(self):
        opened, alt = self._alternative()
        base = [policy_published(), service_committed(), self._outage(), opened, alt]

        reg = replay(base + [
            self._subsidy("FN-1", "2026-08-02"),
            self._subsidy("FN-2", "2026-08-04"),
            self._resume(at="2026-08-03T08:00:00+08:00"),
        ])
        self.assertEqual(codes(reg, "duplicate_subsidy"), {"FN-2"})

        reg = replay(base + [
            self._resume(at="2026-08-03T08:00:00+08:00"),
            self._subsidy("FN-late", "2026-08-05"),
        ])
        self.assertEqual(codes(reg, "duplicate_subsidy"), {"FN-late"})

        # 停用未恢复期间结算一次，随后恢复，是合规路径。
        reg = replay(base + [
            self._subsidy("FN-ok", "2026-08-02"),
            self._resume(at="2026-08-03T08:00:00+08:00"),
        ])
        self.assertNotIn("duplicate_subsidy", codes(reg))


class PrivacyTests(unittest.TestCase):
    def test_small_cell_is_rejected(self):
        events = [
            policy_published(), service_committed(),
            event(
                "FEEDBACK_COLLECTED", "feedback_record", "FB-1",
                {"period_id": "P1", "service_code": "SVC-1", "group": "居民",
                 "satisfaction": 0.8, "concern": "", "cell_count": MIN_CELL_COUNT - 1},
                "FB-1",
            ),
        ]
        self.assertEqual(codes(replay(events)), {"privacy_cell"})

    def test_personal_keys_are_rejected(self):
        events = [
            policy_published(), service_committed(),
            event(
                "USAGE_RECORDED", "usage_record", "U-1",
                {"period_id": "P1", "service_code": "SVC-1", "season": "low",
                 "resident_visits": 10, "tourist_visits": 20, "quota_saturation": 0.2,
                 "id_card": "leaked"},
                "U-1",
            ),
        ]
        self.assertEqual(codes(replay(events)), {"privacy_personal"})


class ReviewTests(unittest.TestCase):
    def _freeze(self, eid="FR-1", period="P1", dataset_hash=FREEZE_HASH, at="2026-09-25T18:00:00+08:00"):
        return event(
            "PERFORMANCE_FROZEN", "performance_period", "period-P1",
            {"period_id": period, "frozen_at": at,
             "event_id_range": ["T-001", "X"], "dataset_hash": dataset_hash},
            eid, at,
        )

    def _assign(self, eid="RV-1", reviewers=("RV-A",), check=True):
        return event(
            "REVIEWER_ASSIGNED", "performance_period", "period-P1",
            {"period_id": "P1", "reviewer_ids": list(reviewers), "conflict_check": check},
            eid, "2026-09-25T18:10:00+08:00",
        )

    def _decide(self, eid="DEC-1", decision="continue", hash_=FREEZE_HASH,
                dimensions=None, services=("SVC-1",)):
        payload = {
            "period_id": "P1",
            "decision_id": eid,
            "service_codes": list(services),
            "frozen_dataset_hash": hash_,
            "dimensions": dimensions or {
                "coverage": 0.8, "fairness": 0.8, "accessibility": 0.8,
                "quality": 0.8, "seasonal_use": 0.8,
            },
            "decision": decision,
        }
        return event("REVIEW_DECIDED", "review_decision", f"decision-{eid}", payload, eid,
                     "2026-09-26T10:00:00+08:00")

    def test_decision_requires_freeze_assignment_and_all_dimensions(self):
        base = [policy_published(), service_committed(), contract()]

        reg = replay(base + [self._assign(), self._decide()])
        self.assertIn("frozen_version", codes(reg))

        reg = replay(base + [self._freeze(), self._decide()])
        self.assertIn("conflict_of_interest", codes(reg))

        reg = replay(base + [self._freeze(), self._assign(), self._decide(hash_="sha256:other")])
        self.assertIn("frozen_version", codes(reg))

        reg = replay(base + [
            self._freeze(), self._assign(),
            self._decide(dimensions={"coverage": 0.8}),
        ])
        self.assertIn("review_dimensions", codes(reg))

        reg = replay(base + [
            self._freeze(), self._assign(),
            self._decide(decision="pause_everything"),
        ])
        self.assertIn("review_decision", codes(reg))

    def test_reviewer_who_is_operator_must_recuse(self):
        base = [policy_published(), service_committed(), contract(), self._freeze()]
        reg = replay(base + [self._assign(reviewers=("OP-1", "RV-A"))])
        self.assertIn("conflict_of_interest", codes(reg))

    def test_post_freeze_writes_are_rejected(self):
        events = [
            policy_published(), service_committed(),
            self._freeze(),
            event(
                "USAGE_RECORDED", "usage_record", "U-late",
                {"period_id": "P1", "service_code": "SVC-1", "season": "low",
                 "resident_visits": 10, "tourist_visits": 10, "quota_saturation": 0.1},
                "U-late", "2026-09-26T08:00:00+08:00",
            ),
        ]
        self.assertEqual(codes(replay(events)), {"frozen_version"})

    def test_irreplaceable_safety_net_cannot_be_withdrawn_for_low_use(self):
        # SVC-1 是公益服务，仅有一条活跃无障碍衔接，淡季低利用。
        events = [
            policy_published(), service_committed(),
            event(
                "LINK_OPENED", "transit_link", "L-1",
                {"link_id": "L-1", "mode": "接驳", "service_codes": ["SVC-1"],
                 "accessible": True, "headway_minutes": 60, "season": "all"},
                "L-1", "2026-01-05T08:00:00+08:00",
            ),
            event(
                "USAGE_RECORDED", "usage_record", "U-low",
                {"period_id": "P1", "service_code": "SVC-1", "season": "low",
                 "resident_visits": 100, "tourist_visits": 10, "quota_saturation": 0.08},
                "U-low", "2026-04-01T09:00:00+08:00",
            ),
            self._freeze(),
            self._assign(),
            self._decide(decision="withdraw"),
        ]
        reg = replay(events)
        self.assertIn("safety_net", codes(reg))


if __name__ == "__main__":
    unittest.main()
