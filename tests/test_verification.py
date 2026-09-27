import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402

DISASTER_TIME = "2026-09-20T00:00:00+00:00"
ZONE_LAT, ZONE_LON, RADIUS = 30.0, 121.0, 50.0


def iso(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat(timespec="seconds")


class EvidenceVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_triaged_claim(self, number="EV-001", lat=ZONE_LAT, lon=ZONE_LON,
                           radius=RADIUS, version=None):
        claim = self.svc.create_claim("intake1", "intake", number, "Q-1", "A区", "flood",
                                      "P-" + number, "R-" + number, lat, lon, 300000)
        claim = self.svc.triage_claim(
            "sup1", "supervisor", claim["id"], claim["version"], 0.1, False,
            zone_latitude=ZONE_LAT, zone_longitude=ZONE_LON,
            zone_radius_km=radius, disaster_time=DISASTER_TIME,
        )
        return claim

    def assign_survey_review(self, claim):
        claim = self.svc.assign_claim("sup1", "supervisor", claim["id"], "adj1", claim["version"], "sur1")
        claim = self.svc.record_survey("adj1", "adjuster", claim["id"], 0.5, "受损", "部分赔付", claim["version"])
        return claim

    def test_in_zone_after_disaster_auto_verified_and_locked(self):
        claim = self.make_triaged_claim()
        result = self.svc.add_evidence(
            "sur1", "surveyor", claim["id"], "a" * 64, "site.jpg", "offline",
            latitude=30.1, longitude=121.1, captured_at=iso(2026, 9, 21, 8), device_id="DEV-1")
        self.assertFalse(result["pending"])
        self.assertEqual("verified", result["evidence"]["status"])
        self.assertEqual(1, result["evidence"]["locked"])
        self.assertEqual([], result["reasons"])

    def test_outside_zone_enters_pending_and_blocks_submit_review(self):
        claim = self.make_triaged_claim()
        claim = self.assign_survey_review(claim)
        result = self.svc.add_evidence(
            "sur1", "surveyor", claim["id"], "b" * 64, "far.jpg", "offline",
            latitude=31.5, longitude=121.0, captured_at=iso(2026, 9, 21, 8), device_id="DEV-2")
        self.assertTrue(result["pending"])
        self.assertEqual("pending", result["evidence"]["status"])
        self.assertEqual(0, result["evidence"]["locked"])
        self.assertTrue(any("灾区" in r for r in result["reasons"]))
        with self.assertRaises(DomainError) as ctx:
            self.svc.submit_review("adj1", "adjuster", claim["id"], claim["version"])
        self.assertEqual(409, ctx.exception.status)
        # 出现在主管待核验清单
        pending = self.svc.pending_evidence("supervisor")
        self.assertEqual(1, len(pending))
        self.assertEqual(result["evidence"]["id"], pending[0]["id"])
        self.assertGreater(pending[0]["distance_km"], RADIUS)

    def test_captured_before_disaster_enters_pending(self):
        claim = self.make_triaged_claim()
        result = self.svc.add_evidence(
            "sur1", "surveyor", claim["id"], "c" * 64, "old.jpg", "offline",
            latitude=30.05, longitude=121.05, captured_at=iso(2026, 9, 19, 23), device_id="DEV-3")
        self.assertTrue(result["pending"])
        self.assertTrue(any("早于灾情" in r for r in result["reasons"]))

    def test_cross_device_jump_within_60_minutes_over_100km(self):
        c1 = self.make_triaged_claim("EV-100", lat=30.0, lon=121.0)
        c2 = self.svc.create_claim("intake1", "intake", "EV-101", "Q-1", "B区", "flood",
                                   "P-EV-101", "R-EV-101", 31.5, 121.0, 200000)
        self.svc.triage_claim("sup1", "supervisor", c2["id"], c2["version"], 0.1)
        # 第一现场：灾区内、灾情后，通过
        self.svc.add_evidence("sur1", "surveyor", c1["id"], "d" * 64, "a.jpg", "offline",
                              latitude=30.0, longitude=121.0,
                              captured_at=iso(2026, 9, 21, 8, 0), device_id="DEV-X")
        # 第二现场：30 分钟后，相距约 167 公里，即便自身位置也在灾区半径内也应待核验
        result = self.svc.add_evidence("sur1", "surveyor", c2["id"], "e" * 64, "b.jpg", "offline",
                                       latitude=31.5, longitude=121.0,
                                       captured_at=iso(2026, 9, 21, 8, 30), device_id="DEV-X")
        self.assertTrue(result["pending"])
        self.assertTrue(any("同一设备" in r for r in result["reasons"]))
        self.assertEqual(1, len(result["cross_device_hits"]))
        self.assertGreaterEqual(result["cross_device_hits"][0]["distance_km"], 100)

    def test_same_device_far_apart_but_beyond_60_minutes_is_fine(self):
        c1 = self.make_triaged_claim("EV-200", lat=30.0, lon=121.0, radius=200)
        c2 = self.svc.create_claim("intake1", "intake", "EV-201", "Q-1", "B区", "flood",
                                   "P-EV-201", "R-EV-201", 30.1, 122.2, 200000)
        self.svc.triage_claim("sup1", "supervisor", c2["id"], c2["version"], 0.1)
        self.svc.add_evidence("sur1", "surveyor", c1["id"], "f" * 64, "a.jpg", "offline",
                              latitude=30.0, longitude=121.0,
                              captured_at=iso(2026, 9, 21, 8, 0), device_id="DEV-Y")
        result = self.svc.add_evidence("sur1", "surveyor", c2["id"], "0" * 63 + "1", "b.jpg", "offline",
                                       latitude=30.1, longitude=122.2,
                                       captured_at=iso(2026, 9, 21, 9, 1), device_id="DEV-Y")
        self.assertFalse(result["pending"])
        self.assertEqual([], result["cross_device_hits"])

    def test_confirm_then_locked_and_submit_review_allowed(self):
        claim = self.make_triaged_claim()
        claim = self.assign_survey_review(claim)
        ev = self.svc.add_evidence("sur1", "surveyor", claim["id"], "b" * 64, "far.jpg", "offline",
                                   latitude=31.5, longitude=121.0,
                                   captured_at=iso(2026, 9, 21, 8), device_id="DEV-2")
        verified = self.svc.verify_evidence("sup1", "supervisor", ev["evidence"]["id"], "confirmed",
                                            reason="人工核对现场，定位漂移")
        self.assertEqual("verified", verified["status"])
        self.assertEqual(1, verified["locked"])
        self.assertEqual("sup1", verified["reviewed_by"])
        # 已锁定证据不能再次核验
        with self.assertRaises(DomainError) as ctx:
            self.svc.verify_evidence("sup1", "supervisor", ev["evidence"]["id"], "rejected", reason="x")
        self.assertEqual(409, ctx.exception.status)
        claim = self.svc.submit_review("adj1", "adjuster", claim["id"], claim["version"])
        self.assertEqual("review", claim["status"])

    def test_reject_requires_reason_keeps_raw_evidence_and_blocks(self):
        claim = self.make_triaged_claim()
        claim = self.assign_survey_review(claim)
        ev = self.svc.add_evidence("sur1", "surveyor", claim["id"], "b" * 64, "far.jpg", "offline",
                                   latitude=31.5, longitude=121.0,
                                   captured_at=iso(2026, 9, 21, 8), device_id="DEV-2")
        with self.assertRaises(DomainError) as ctx:
            self.svc.verify_evidence("sup1", "supervisor", ev["evidence"]["id"], "rejected")
        self.assertEqual(400, ctx.exception.status)
        rejected = self.svc.verify_evidence("sup1", "supervisor", ev["evidence"]["id"], "rejected",
                                            reason="拍摄位置明显不在灾区")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(1, rejected["locked"])
        # 原始证据仍保留，且经纬度/拍摄时间/设备号未被改动
        self.assertEqual("b" * 64, rejected["sha256"])
        self.assertEqual("DEV-2", rejected["device_id"])
        self.assertEqual(31.5, rejected["latitude"])
        self.assertEqual("拍摄位置明显不在灾区", rejected["review_reason"])

    def test_only_supervisor_verifies_and_views_pending(self):
        claim = self.make_triaged_claim()
        ev = self.svc.add_evidence("sur1", "surveyor", claim["id"], "b" * 64, "far.jpg", "offline",
                                   latitude=31.5, longitude=121.0,
                                   captured_at=iso(2026, 9, 21, 8), device_id="DEV-2")
        with self.assertRaises(DomainError) as ctx:
            self.svc.verify_evidence("sur1", "surveyor", ev["evidence"]["id"], "confirmed")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.svc.pending_evidence("adjuster")
        self.assertEqual(403, ctx.exception.status)

    def test_partial_geo_fields_rejected(self):
        claim = self.make_triaged_claim()
        with self.assertRaises(DomainError) as ctx:
            self.svc.add_evidence("sur1", "surveyor", claim["id"], "a" * 64, "x.jpg", "offline",
                                  latitude=30.1, longitude=121.1, captured_at=iso(2026, 9, 21))
        self.assertEqual(400, ctx.exception.status)

    def test_legacy_hash_only_evidence_still_works(self):
        claim = self.make_triaged_claim()
        claim = self.assign_survey_review(claim)
        result = self.svc.add_evidence("adj1", "adjuster", claim["id"], "9" * 64, "old.png", "legacy")
        self.assertFalse(result["pending"])
        self.assertEqual("verified", result["evidence"]["status"])
        self.svc.submit_review("adj1", "adjuster", claim["id"], claim["version"])

    def test_zone_registered_via_triage_and_dedicated_endpoint(self):
        claim = self.svc.create_claim("intake1", "intake", "EV-300", "Q-9", "A区", "flood",
                                      "P-1", "R-1", 30.0, 121.0, 1000)
        self.svc.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1,
                              zone_latitude=30.0, zone_longitude=121.0,
                              zone_radius_km=30, disaster_time=DISASTER_TIME)
        zone = self.svc.register_zone("sup1", "supervisor", "Q-9", "A区扩大",
                                      30.0, 121.0, 80.0, DISASTER_TIME)
        self.assertEqual(80.0, zone["radius_km"])
        # 同 event_id 不产生第二条
        state = self.svc.state("sup1", "supervisor")
        self.assertEqual(1, len([z for z in state["zones"] if z["event_id"] == "Q-9"]))

    def test_invalid_coordinates_and_time(self):
        with self.assertRaises(DomainError):
            self.svc.register_zone("sup1", "supervisor", "Q-X", "X", 999, 121, 10, DISASTER_TIME)
        with self.assertRaises(DomainError):
            self.svc.register_zone("sup1", "supervisor", "Q-X", "X", 30, 121, 0, DISASTER_TIME)
        with self.assertRaises(DomainError):
            self.svc.register_zone("sup1", "supervisor", "Q-X", "X", 30, 121, 10, "not-a-time")


if __name__ == "__main__":
    unittest.main()
