from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inclusive_tourism_services.contracts import validate_event
from inclusive_tourism_services.ledger import (
    assess_outages,
    benefit_violations,
    breakdown_funds,
    capacity_violations,
    check_capacity,
    decision_governance_violations,
    evidence_scope_violations,
    freeze_period,
    fund_accounting_issues,
    investment_journey_explanations,
    outage_violations,
    privacy_violations,
    review_findings,
    score_services,
    verify_benefits,
)


def evt(
    event_id: str,
    event_type: str,
    aggregate_id: str,
    *,
    aggregate_type: str = "public_facility",
    occurred_at: str = "2026-08-01T10:00:00+08:00",
    version: int = 1,
    **details,
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "summary": event_id,
        "details": details,
    }


class LedgerSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.facts = json.loads((ROOT / "data" / "ledger_sample.json").read_text(encoding="utf-8"))
        cls.schema = json.loads(
            (ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )

    def test_every_sample_event_satisfies_contract(self) -> None:
        for fact in self.facts:
            self.assertEqual([], validate_event(fact, self.schema), fact["event_id"])

    def test_sample_has_no_rule_violations(self) -> None:
        report = review_findings(self.facts)
        self.assertEqual([], report["issues"], [i.message for i in report["issues"]])

    def test_fund_breakdown_keeps_three_ledgers_separate(self) -> None:
        breakdown = breakdown_funds(self.facts)
        self.assertEqual(500_000_000, breakdown.fiscal)
        self.assertEqual(201_200_000, breakdown.purchase_of_service)
        self.assertEqual(80_000_000, breakdown.operating_revenue)
        self.assertEqual(701_200_000, breakdown.public_total)
        self.assertEqual(781_200_000, breakdown.total)

    def test_freeze_digest_matches_decision_version(self) -> None:
        manifest = freeze_period(self.facts, "2026Q3")
        freeze = next(f for f in self.facts if f["event_type"] == "PERFORMANCE_FROZEN")
        decision = next(f for f in self.facts if f["event_type"] == "DECISION_MADE")
        self.assertEqual(freeze["details"]["data_digest"], manifest.digest)
        self.assertEqual(decision["details"]["based_on_digest"], manifest.digest)
        self.assertEqual(25, manifest.event_count)

    def test_sample_surfaces_the_two_policy_dilemmas_as_tags(self) -> None:
        scores = {s.facility_id: s for s in score_services(self.facts)}
        # 新游客中心：居民休憩空间被挤占 → 居民负担增加
        self.assertIn("resident_burden_increased", scores["visitor-center-01"].tags)
        self.assertNotIn("low_season_idle", scores["visitor-center-01"].tags)
        # 淡季低利用的无障碍接驳线承担兜底职责，不得被判为闲置
        self.assertIn("safety_net_low_utilization", scores["accessible-shuttle-01"].tags)
        self.assertNotIn("low_season_idle", scores["accessible-shuttle-01"].tags)
        self.assertIn("equitable_access", scores["accessible-shuttle-01"].tags)

    def test_investments_explain_journey_without_individuals(self) -> None:
        investments = investment_journey_explanations(self.facts)
        self.assertEqual(1, len(investments))
        item = investments[0]
        self.assertEqual("到达", item["journey_stage"])
        self.assertEqual(("mobility_impaired", "elderly"), item["beneficiary_groups"])
        self.assertNotIn("beneficiary_ref", item)


class CapacityProtectionTests(unittest.TestCase):
    def test_commercial_booking_displacing_public_quota_is_flagged(self) -> None:
        facts = [
            evt(
                "c1",
                "CAPACITY_REPORTED",
                "fac-1",
                total=100,
                public_reserved=60,
                public_used=40,
                commercial_accepted=70,
            )
        ]
        check = check_capacity(facts)[0]
        # 商业渠道只有 40 个非保留席位，却接受 70 → 挤占 30
        self.assertEqual(30, check.displaced)
        codes = {i.code for i in capacity_violations(facts)}
        self.assertIn("public_capacity_displaced", codes)

    def test_commercial_within_commercial_headroom_is_fine(self) -> None:
        facts = [
            evt(
                "c2",
                "CAPACITY_REPORTED",
                "fac-2",
                total=100,
                public_reserved=60,
                public_used=20,
                commercial_accepted=40,
            )
        ]
        self.assertEqual(0, check_capacity(facts)[0].displaced)
        self.assertEqual([], capacity_violations(facts))

    def test_reserved_larger_than_total_is_flagged(self) -> None:
        facts = [
            evt(
                "c3",
                "CAPACITY_REPORTED",
                "fac-3",
                total=50,
                public_reserved=80,
                public_used=0,
                commercial_accepted=0,
            )
        ]
        self.assertIn("reserved_exceeds_total", {i.code for i in capacity_violations(facts)})


class FundAccountingTests(unittest.TestCase):
    def test_unknown_source_and_non_positive_amount_are_flagged(self) -> None:
        facts = [
            evt("f1", "FUNDS_ALLOCATED", "a1", source="sponsorship", amount=100),
            evt("f2", "FUNDS_ALLOCATED", "a2", source="fiscal", amount=0),
        ]
        codes = {i.code for i in fund_accounting_issues(facts)}
        self.assertIn("unknown_fund_source", codes)
        self.assertIn("amount_must_be_positive_integer", codes)

    def test_subsidy_cannot_be_paid_from_operating_revenue(self) -> None:
        facts = [
            evt(
                "f3",
                "SUBSIDY_PAID",
                "a3",
                source="operating_revenue",
                amount=100,
                service_id="s1",
            )
        ]
        self.assertIn("subsidy_from_revenue", {i.code for i in fund_accounting_issues(facts)})


class BenefitVerificationTests(unittest.TestCase):
    GRANT = evt(
        "g1",
        "BENEFIT_GRANTED",
        "ent-1",
        aggregate_type="benefit_entitlement",
        entitlement_id="ent-1",
        beneficiary_ref="cohort-a",
        kind="voucher",
        eligible_qualification="resident_low_income",
        effective_from="2026-06-01",
        effective_to="2026-09-30",
        channel="counter",
    )

    def redemption(self, event_id: str, **overrides) -> dict:
        payload = dict(
            entitlement_id="ent-1",
            beneficiary_ref="cohort-a",
            kind="voucher",
            qualification="resident_low_income",
            channel="mini_program",
            service_id="svc-1",
            redeemed_at="2026-07-01",
        )
        payload.update(overrides)
        return evt(
            event_id,
            "BENEFIT_REDEEMED",
            "ent-1",
            aggregate_type="benefit_entitlement",
            **payload,
        )

    def test_valid_redemption_passes(self) -> None:
        facts = [self.GRANT, self.redemption("r1")]
        result = verify_benefits(facts)[0]
        self.assertTrue(result.eligible)
        self.assertTrue(result.within_effective_period)
        self.assertFalse(result.duplicate)
        self.assertEqual([], benefit_violations(facts))

    def test_wrong_qualification_and_expired_period_are_flagged(self) -> None:
        facts = [self.GRANT, self.redemption("r2", qualification="tourist", redeemed_at="2026-10-05")]
        result = verify_benefits(facts)[0]
        self.assertFalse(result.eligible)
        self.assertFalse(result.within_effective_period)
        codes = {i.code for i in benefit_violations(facts)}
        self.assertIn("ineligible_redemption", codes)
        self.assertIn("outside_effective_period", codes)

    def test_same_benefit_redeemed_twice_across_channels_is_duplicate(self) -> None:
        facts = [
            self.GRANT,
            self.redemption("r3", channel="mini_program"),
            self.redemption("r4", channel="counter"),
        ]
        results = verify_benefits(facts)
        self.assertFalse(results[0].duplicate)
        self.assertTrue(results[1].duplicate)
        self.assertIn("duplicate_benefit", {i.code for i in benefit_violations(facts)})

    def test_redemption_without_grant_is_ineligible(self) -> None:
        facts = [self.redemption("r5")]
        self.assertFalse(verify_benefits(facts)[0].eligible)


class OutageTests(unittest.TestCase):
    def outage_facts(self, *, subsidy_count: int = 1, with_alternative: bool = True) -> list[dict]:
        facts = [
            evt(
                "o1",
                "OUTAGE_DECLARED",
                "out-1",
                aggregate_type="outage_record",
                facility_id="fac-1",
                affected_service_ids=["s1"],
                all_service_ids=["s1", "s2"],
            )
        ]
        if with_alternative:
            facts.append(
                evt(
                    "o2",
                    "ALTERNATIVE_ARRANGED",
                    "out-1",
                    aggregate_type="outage_record",
                    outage_id="out-1",
                    alternative_service_id="s2",
                )
            )
        facts.append(
            evt(
                "o3",
                "OUTAGE_RESOLVED",
                "out-1",
                aggregate_type="outage_record",
                outage_id="out-1",
                resumed_service_ids=["s1"],
            )
        )
        for index in range(subsidy_count):
            facts.append(
                evt(
                    f"p{index}",
                    "SUBSIDY_PAID",
                    f"acct-{index}",
                    source="purchase_of_service",
                    amount=100,
                    outage_id="out-1",
                    service_id="s1",
                )
            )
        return facts

    def test_only_affected_service_is_suspended(self) -> None:
        assessment = assess_outages(self.outage_facts())[0]
        self.assertEqual(("s1",), assessment.suspended_service_ids)
        self.assertEqual(("s2",), assessment.unaffected_service_ids)
        self.assertEqual("s2", assessment.alternative_service_id)
        self.assertTrue(assessment.resumed)
        self.assertEqual([], outage_violations(self.outage_facts()))

    def test_missing_alternative_arrangement_is_flagged(self) -> None:
        facts = self.outage_facts(with_alternative=False)
        self.assertIn("alternative_missing", {i.code for i in outage_violations(facts)})

    def test_duplicate_recovery_subsidy_is_flagged(self) -> None:
        facts = self.outage_facts(subsidy_count=2)
        self.assertTrue(assess_outages(facts)[0].duplicate_subsidy)
        self.assertIn("duplicate_recovery_subsidy", {i.code for i in outage_violations(facts)})

    def test_suspension_beyond_affected_scope_is_flagged(self) -> None:
        facts = [
            evt(
                "o4",
                "OUTAGE_DECLARED",
                "out-2",
                aggregate_type="outage_record",
                affected_service_ids=["s9"],
                all_service_ids=["s1", "s2"],
            )
        ]
        self.assertIn("suspension_outside_scope", {i.code for i in outage_violations(facts)})


class EvidenceScopeTests(unittest.TestCase):
    CONTRACT = evt(
        "k1",
        "CONTRACT_SIGNED",
        "con-1",
        aggregate_type="operation_contract",
        operator_id="op-1",
        facility_ids=["fac-1"],
        service_ids=["svc-1"],
    )

    def test_operator_without_contract_is_flagged(self) -> None:
        facts = [
            evt(
                "e1",
                "EVIDENCE_SUBMITTED",
                "con-x",
                aggregate_type="operation_contract",
                operator_id="op-x",
                facility_id="fac-1",
            )
        ]
        self.assertIn("operator_without_contract", {i.code for i in evidence_scope_violations(facts)})

    def test_evidence_for_out_of_scope_facility_and_service_is_flagged(self) -> None:
        facts = [
            self.CONTRACT,
            evt(
                "e2",
                "EVIDENCE_SUBMITTED",
                "con-1",
                aggregate_type="operation_contract",
                operator_id="op-1",
                facility_id="fac-2",
                service_id="svc-2",
            ),
        ]
        codes = {i.code for i in evidence_scope_violations(facts)}
        self.assertIn("facility_outside_contract", codes)
        self.assertIn("service_outside_contract", codes)

    def test_evidence_within_contract_passes(self) -> None:
        facts = [
            self.CONTRACT,
            evt(
                "e3",
                "EVIDENCE_SUBMITTED",
                "con-1",
                aggregate_type="operation_contract",
                operator_id="op-1",
                facility_id="fac-1",
                service_id="svc-1",
            ),
        ]
        self.assertEqual([], evidence_scope_violations(facts))


class GovernanceTests(unittest.TestCase):
    def freeze(self, digest: str = "digest-1", reviewers=None) -> dict:
        return evt(
            "z1",
            "PERFORMANCE_FROZEN",
            "P1",
            aggregate_type="performance_period",
            occurred_at="2026-09-24T12:00:00+08:00",
            period_id="P1",
            data_digest=digest,
            reviewer_ids=reviewers or ["rv-1"],
        )

    def decision(self, **details) -> dict:
        payload = dict(
            period_id="P1",
            based_on_digest="digest-1",
            decider_id="rv-1",
            interested_party_ids=["op-1"],
        )
        payload.update(details)
        return evt(
            "z2",
            "DECISION_MADE",
            "P1",
            aggregate_type="performance_period",
            occurred_at="2026-09-25T10:00:00+08:00",
            **payload,
        )

    def test_valid_frozen_decision_passes_and_digest_is_deterministic(self) -> None:
        facts = [self.freeze(), self.decision()]
        manifest = freeze_period(facts, "P1")
        self.assertEqual(manifest.digest, freeze_period(deepcopy(facts), "P1").digest)
        self.assertEqual([], decision_governance_violations(facts))

    def test_unfrozen_period_is_flagged(self) -> None:
        self.assertIn(
            "period_not_frozen",
            {i.code for i in decision_governance_violations([self.decision()])},
        )

    def test_digest_mismatch_is_flagged(self) -> None:
        facts = [self.freeze(digest="digest-1"), self.decision(based_on_digest="digest-tampered")]
        self.assertIn("digest_mismatch", {i.code for i in decision_governance_violations(facts)})

    def test_interested_party_must_recuse(self) -> None:
        facts = [self.freeze(), self.decision(decider_id="op-1")]
        self.assertIn("conflicted_decider", {i.code for i in decision_governance_violations(facts)})

    def test_decider_must_be_registered_reviewer(self) -> None:
        facts = [self.freeze(), self.decision(decider_id="rv-outsider")]
        self.assertIn(
            "decider_not_among_reviewers",
            {i.code for i in decision_governance_violations(facts)},
        )

    def test_freeze_without_freeze_event_raises(self) -> None:
        with self.assertRaises(KeyError):
            freeze_period([], "P1")


class PrivacyTests(unittest.TestCase):
    def test_bucket_below_k_is_suppressed(self) -> None:
        facts = [
            evt(
                "u1",
                "USAGE_AGGREGATED",
                "u-1",
                aggregate_type="usage_observation",
                facility_id="fac-1",
                group_buckets=[{"group": "residents", "count": 30}, {"group": "rare", "count": 2}],
            )
        ]
        issues = privacy_violations(facts, k=5)
        self.assertEqual(["bucket_below_k"], [i.code for i in issues])

    def test_feedback_without_buckets_is_flagged(self) -> None:
        facts = [
            evt(
                "u2",
                "FEEDBACK_COLLECTED",
                "f-1",
                aggregate_type="feedback_record",
            )
        ]
        self.assertIn("group_buckets_required", {i.code for i in privacy_violations(facts)})


class ReviewInsightTests(unittest.TestCase):
    def usage(self, event_id: str, facility: str, season: str, count: int, **extra) -> dict:
        payload = dict(
            facility_id=facility,
            season=season,
            total_count=count,
            metrics={"coverage": 0.7, "fairness": 0.7, "accessibility": 0.7, "quality": 0.7},
            group_buckets=[{"group": "all", "count": 10}],
        )
        payload.update(extra)
        return evt(
            event_id,
            "USAGE_AGGREGATED",
            f"obs-{event_id}",
            aggregate_type="usage_observation",
            **payload,
        )

    def test_low_off_season_non_safety_net_is_idle(self) -> None:
        facts = [self.usage("u1", "fac-idle", "peak", 1000), self.usage("u2", "fac-idle", "off", 100)]
        tags = score_services(facts)[0].tags
        self.assertIn("low_season_idle", tags)
        self.assertNotIn("safety_net_low_utilization", tags)

    def test_unresolved_outage_is_maintenance_gap(self) -> None:
        facts = [
            self.usage("u3", "fac-maint", "peak", 1000),
            self.usage("u4", "fac-maint", "off", 500),
            evt(
                "u5",
                "OUTAGE_DECLARED",
                "out-open",
                aggregate_type="outage_record",
                facility_id="fac-maint",
                affected_service_ids=["s1"],
                all_service_ids=["s1"],
            ),
        ]
        self.assertIn("maintenance_gap", score_services(facts)[0].tags)

    def test_overall_score_weighs_all_five_dimensions(self) -> None:
        facts = [
            self.usage(
                "u6",
                "fac-score",
                "peak",
                100,
                metrics={"coverage": 1.0, "fairness": 1.0, "accessibility": 1.0, "quality": 1.0},
            )
        ]
        score = score_services(facts)[0]
        # 旺季无淡季观测时兜底折算为 0.4，综合分 = 0.25+0.2+0.2+0.2+0.15*0.4
        self.assertAlmostEqual(0.91, score.overall, places=3)


if __name__ == "__main__":
    unittest.main()
