import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402

ZONE = {"disaster_lat": 30.0, "disaster_lon": 121.0,
        "disaster_time": "2026-09-20T08:00:00Z", "radius_km": 50}


class FieldEvidenceVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, number, lat=30.0, lon=121.0):
        return self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", "P-" + number,
            "R-" + number, lat, lon, 300000, False, False,
        )

    def surveyed(self, number, lat=30.0, lon=121.0):
        claim = self.claim(number, lat, lon)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, False, **ZONE)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        return claim

    def field(self, claim_id, digest, lat=30.01, lon=121.01, captured="2026-09-21T10:00:00Z", device="DEV-1"):
        return self.service.add_evidence(
            "adjuster1", "adjuster", claim_id, digest, "site.jpg", "field-offline",
            latitude=lat, longitude=lon, captured_at=captured, device_id=device,
        )

    def test_triage_registers_zone_and_clean_evidence_auto_verified(self):
        claim = self.surveyed("V-001")
        zones = self.service.state("sup1", "supervisor")["zones"]
        self.assertEqual(1, len(zones))
        self.assertEqual(50, zones[0]["radius_km"])
        result = self.field(claim["id"], "a" * 64)
        self.assertEqual("verified", result["verification"]["status"])
        self.assertEqual([], result["verification"]["flags"])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.5, "房屋进水", "部分赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        self.assertEqual("review", claim["status"])

    def test_outside_zone_and_before_disaster_block_review_until_confirmed(self):
        claim = self.surveyed("V-002")
        far = self.field(claim["id"], "b" * 64, lat=31.5, lon=121.0)
        self.assertEqual("pending", far["verification"]["status"])
        self.assertEqual(["outside_zone"], [f["code"] for f in far["verification"]["flags"]])
        early = self.field(claim["id"], "c" * 64, captured="2026-09-19T07:00:00Z")
        self.assertEqual("pending", early["verification"]["status"])
        self.assertEqual(["before_disaster"], [f["code"] for f in early["verification"]["flags"]])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.5, "房屋进水", "部分赔付", claim["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        self.assertEqual(409, ctx.exception.status)
        pending = self.service.pending_verifications("sup1", "supervisor")
        self.assertEqual(2, len(pending))
        self.assertEqual("V-002", pending[0]["claim_no"])
        for item in pending:
            locked = self.service.verify_evidence("sup1", "supervisor", item["id"], "confirm")
            self.assertEqual("verified", locked["status"])
            self.assertEqual("sup1", locked["verified_by"])
        with self.assertRaises(DomainError) as ctx2:
            self.service.verify_evidence("sup1", "supervisor", pending[0]["id"], "confirm")
        self.assertEqual(409, ctx2.exception.status)
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        self.assertEqual("review", claim["status"])

    def test_device_jump_across_claims_flagged(self):
        near = self.surveyed("V-010")
        far = self.surveyed("V-011", lat=32.0, lon=123.0)
        self.service.register_disaster_zone("sup1", "supervisor", far["id"], 32.0, 123.0,
                                            "2026-09-20T08:00:00Z", 50)
        first = self.field(near["id"], "d" * 64, device="DEV-9")
        self.assertEqual("verified", first["verification"]["status"])
        second = self.field(far["id"], "e" * 64, lat=32.0, lon=123.0,
                            captured="2026-09-21T10:30:00Z", device="DEV-9")
        self.assertEqual("pending", second["verification"]["status"])
        self.assertEqual(["device_jump"], [f["code"] for f in second["verification"]["flags"]])

    def test_reject_keeps_evidence_with_reason_and_unblocks_review(self):
        claim = self.surveyed("V-020")
        flagged = self.field(claim["id"], "f" * 64, lat=32.0, lon=121.0)
        evidence_id = flagged["evidence"]["id"]
        with self.assertRaises(DomainError) as ctx:
            self.service.verify_evidence("sup1", "supervisor", evidence_id, "reject")
        self.assertEqual(400, ctx.exception.status)
        rejected = self.service.verify_evidence("sup1", "supervisor", evidence_id, "reject", "  拍摄地不在灾区，疑似张冠李戴  ")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("拍摄地不在灾区，疑似张冠李戴", rejected["reject_reason"])
        kept = [e for e in self.service.state("sup1", "supervisor")["evidence"] if e["id"] == evidence_id]
        self.assertEqual(1, len(kept))
        self.assertEqual("rejected", kept[0]["status"])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.5, "房屋进水", "部分赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        self.assertEqual("review", claim["status"])

    def test_field_evidence_requires_full_geo_payload_and_supervisor_verify(self):
        claim = self.surveyed("V-030")
        with self.assertRaises(DomainError) as ctx:
            self.service.add_evidence("adjuster1", "adjuster", claim["id"], "0" * 64, "x.jpg", "field-offline",
                                      latitude=30.01, longitude=121.01, captured_at="2026-09-21T10:00:00Z")
        self.assertEqual(400, ctx.exception.status)
        flagged = self.field(claim["id"], "1" * 64, lat=32.0, lon=121.0)
        with self.assertRaises(DomainError) as ctx2:
            self.service.verify_evidence("adjuster1", "adjuster", flagged["evidence"]["id"], "confirm")
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(DomainError) as ctx3:
            self.service.pending_verifications("adjuster1", "adjuster")
        self.assertEqual(403, ctx3.exception.status)

    def test_register_zone_endpoint_and_terminal_guard(self):
        claim = self.claim("V-040")
        zone = self.service.register_disaster_zone("sup1", "supervisor", claim["id"], 30.0, 121.0,
                                                   "2026-09-20T08:00:00+00:00", 80)
        self.assertEqual(80, zone["radius_km"])
        with self.assertRaises(DomainError) as ctx:
            self.service.register_disaster_zone("adjuster1", "adjuster", claim["id"], 30.0, 121.0,
                                                "2026-09-20T08:00:00Z", 80)
        self.assertEqual(403, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
