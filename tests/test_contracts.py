from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inclusive_tourism_services.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_reported_in_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_naive_time_and_zero_version_are_rejected(self) -> None:
        payload = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(payload, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_unknown_event_type_is_rejected(self) -> None:
        payload = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(payload, self.schema)
        self.assertEqual([("event_type", "unsupported_value")], [(item.field, item.code) for item in issues])

    def test_event_payload_is_required_and_object(self) -> None:
        payload = dict(self.sample)
        del payload["event_payload"]
        self.assertIn(
            ("event_payload", "required"),
            {(i.field, i.code) for i in validate_event(payload, self.schema)},
        )
        payload = dict(self.sample, event_payload="not-an-object")
        self.assertIn(
            ("event_payload", "object_required"),
            {(i.field, i.code) for i in validate_event(payload, self.schema)},
        )

    def test_event_aggregate_pairing_is_enforced(self) -> None:
        payload = dict(
            self.sample,
            event_type="CAPACITY_REPORTED",
            aggregate_type="transit_link",
            event_payload={
                "facility_id": "F1",
                "total_capacity": 10,
                "public_quota": 6,
                "commercial_quota": 4,
                "commercial_bookings": 0,
                "as_of": "2026-01-01",
            },
        )
        self.assertIn(
            ("aggregate_type", "aggregate_mismatch"),
            {(i.field, i.code) for i in validate_event(payload, self.schema)},
        )

    def test_payload_required_fields_are_checked_per_event_type(self) -> None:
        payload = dict(
            self.sample,
            event_type="FUNDING_ALLOCATED",
            aggregate_type="funding_ledger",
            event_payload={"funding_id": "F1"},
        )
        missing = {
            i.field for i in validate_event(payload, self.schema) if i.code == "payload_required"
        }
        for field in ("fiscal_period", "source", "amount", "service_code"):
            self.assertIn(f"event_payload.{field}", missing)

    def test_sample_ledger_events_are_all_envelope_valid(self) -> None:
        events = json.loads((ROOT / "data" / "sample_ledger.json").read_text(encoding="utf-8"))
        violations = [
            (event["event_id"], issue.field, issue.code)
            for event in events
            for issue in validate_event(event, self.schema)
        ]
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
