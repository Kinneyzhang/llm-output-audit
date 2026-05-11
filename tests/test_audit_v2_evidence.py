import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audit_v2", ROOT / "scripts" / "audit_v2.py")
audit_v2 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit_v2)


class AuditV2EvidenceTests(unittest.TestCase):
    def test_dgx_spark_is_public_and_official_specs_support_claim(self):
        old_fetch = audit_v2.fetch_text_url
        try:
            audit_v2.fetch_text_url = lambda url, timeout=8: (
                "NVIDIA DGX Spark Specifications Architecture NVIDIA Grace Blackwell GPU. "
                "Powered by the NVIDIA GB10 Grace Blackwell Superchip. "
                "System Memory 128 GB LPDDR5x, coherent unified system memory. "
                "Memory Bandwidth 273 GB/s. Tensor Performance Up to 1 PFLOP FP4."
            )
            text = "NVIDIA DGX Spark 搭载 GB10 Grace Blackwell Superchip，拥有 128GB LPDDR5X 统一内存，内存带宽 273 GB/s，FP4 算力 1 PFLOPS。"
            self.assertEqual(audit_v2.infer_verifiability(text), "public")
            claim = {"claim_id": "c-001", "claim_text": text}
            evidence = audit_v2.specialized_evidence(claim)
            self.assertTrue(any(item["source_type"] == "official_product_page" and item["supports"] == ["c-001"] for item in evidence))
        finally:
            audit_v2.fetch_text_url = old_fetch

    def test_emacs_lisp_line_count_can_refute_bad_estimate(self):
        old_counter = audit_v2.github_archive_line_count
        try:
            audit_v2.github_archive_line_count = lambda repo, ref, prefix, suffixes: (1591, 1700610, "https://example.test/emacs.tar.gz")
            claim = {"claim_id": "c-007", "claim_text": "GNU Emacs 官方源码 lisp/ 目录预计约 40 万行。"}
            evidence = audit_v2.specialized_evidence(claim)
            self.assertEqual(evidence[0]["source_type"], "github_archive_line_count")
            self.assertEqual(evidence[0]["contradicts"], ["c-007"])
        finally:
            audit_v2.github_archive_line_count = old_counter

    def test_missing_reason_prefers_informative_evidence_over_source_error(self):
        claims = [{"claim_id": "c-008", "claim_text": "MELPA 全部包预计约 200 万行。", "verifiability": "public"}]
        evidence = [
            audit_v2.source_error_record(claims[0], "tavily", "Tavily quota exceeded"),
            {
                "evidence_id": "stat-melpa-recipes-c-008",
                "claim_id": "c-008",
                "source_type": "github_tree_metadata",
                "authority": "canonical",
                "quote": "MELPA recipe repository contains 6222 recipe files; total package LOC requires crawling upstream repos.",
                "supports": [],
                "contradicts": [],
                "missing": ["c-008"],
                "scores": {"retrieval_relevance": 0.85},
            },
        ]
        verdict = audit_v2.verdicts_from_evidence(claims, evidence)[0]
        self.assertEqual(verdict["truth_verdict"], "not_enough_evidence")
        self.assertIn("MELPA recipe repository", verdict["reason"])
        self.assertNotIn("Tavily quota", verdict["reason"])


if __name__ == "__main__":
    unittest.main()
