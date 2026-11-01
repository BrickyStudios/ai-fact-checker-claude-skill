"""Run: python3 -m unittest discover tests"""
import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factcheck", Path(__file__).parent.parent / "skills/fact-checker/scripts/factcheck.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)


class Numbers(unittest.TestCase):
    def test_locale_formats_are_one_number(self):
        for token in ("1,234.5", "1.234,5", "1 234,5", "1'234.5"):
            self.assertEqual(fc.to_decimal(token), Decimal("1234.5"), token)

    def test_thousands_versus_decimals(self):
        self.assertEqual(fc.to_decimal("1,234"), Decimal(1234))
        self.assertEqual(fc.to_decimal("1.234.567"), Decimal(1234567))
        self.assertEqual(fc.to_decimal("1,5"), Decimal("1.5"))
        self.assertEqual(fc.to_decimal("0,125"), Decimal("0.125"))


class Report(unittest.TestCase):
    def test_report_names_its_maker(self):
        gate = {"doc": {"source": "a.pdf"}, "coverage": {"covered": 0, "required": 0, "percent": 100.0, "untouched": []}}
        self.assertIn(fc.HOMEPAGE, fc.render_markdown([], gate))


class Highlights(unittest.TestCase):
    def test_quote_ending_before_punctuation_is_found_across_lines(self):
        class Page:
            def extract_words(self, **_):
                return [{"text": t, "x0": i, "x1": i + 1, "top": top, "bottom": top + 1}
                        for i, (t, top) in enumerate([("rose", 0), ("14.8%", 0), ("to", 12), ("million.", 12)])]
        self.assertEqual(len(fc.find_quads(Page(), "rose 14.8% to million")), 2)


class Calc(unittest.TestCase):
    def test_rounding_is_half_up_on_exact_decimals(self):
        self.assertTrue(fc.calc_result("(616-514)/514*100", 19.8, 1)["matches"])
        self.assertFalse(fc.calc_result("(616-514)/514*100", 21.8, 1)["matches"])
        self.assertTrue(fc.calc_result("1/8", 0.13, 2)["matches"])  # float would give 0.12

    def test_refuses_anything_but_arithmetic(self):
        for expr in ("__import__('os').system('id')", "open('x')", "(1).__class__", "9**9**9"):
            with self.assertRaises((ValueError, SyntaxError, ArithmeticError), msg=expr):
                fc.evaluate(expr)

    def test_operands_ignore_percent_scaling(self):
        self.assertEqual(fc.operands("(5-4)/4*100"), {Decimal(5), Decimal(4)})


class Normalize(unittest.TestCase):
    def test_folds_pdf_artifacts(self):
        self.assertEqual(fc.normalize("Bench­mark"), fc.normalize("Benchmark"))
        self.assertEqual(fc.normalize("re-\nport “x”"), fc.normalize("report \"x\""))
        self.assertEqual(fc.normalize("10–2 GW"), fc.normalize("10-2 GW"))


class HtmlText(unittest.TestCase):
    def test_drops_scripts_keeps_entities(self):
        self.assertEqual(fc.html_to_text("<script>x()</script><p>A &amp; B</p>"), "A & B")


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "sources").mkdir()
        pages = ["Revenue rose 10% to EUR 110 million.\nDirect sales were strong in Norway.",
                 "Income statement: revenue 110 (2025), 105 (2024).\nWe love bread."]
        self.doc = {"source": "x.txt", "words": 10, "pages": pages, "sentences": fc.sentences_of(pages)}
        (self.work / "doc.json").write_text(json.dumps(self.doc))
        self.claim = {
            "id": "C1", "page": 1, "quote": "Revenue rose 10% to EUR 110 million", "verdict": "wrong",
            "correction": "Revenue rose 4.8%",
            "calc": {"expr": "(110-105)/105*100", "stated": 10, "decimals": 1},
            "evidence": [{"kind": "document", "page": 2, "quote": "revenue 110 (2025), 105 (2024)"}],
        }

    def check(self, claim):
        return fc.check_claim(claim, self.doc, self.work)

    def test_correct_finding_passes(self):
        self.assertEqual(self.check(self.claim), [])

    def test_invented_claim_quote_fails(self):
        self.claim["quote"] = "Revenue rose 99% to EUR 110 million"
        self.assertTrue(any("not verbatim" in p for p in self.check(self.claim)))

    def test_invented_evidence_fails(self):
        self.claim["evidence"][0]["quote"] = "revenue 110 (2025), 100 (2024)"
        self.assertTrue(self.check(self.claim))

    def test_invented_calc_input_fails(self):
        self.claim["calc"]["expr"] = "(110-99)/99*100"
        self.assertTrue(any("calc inputs" in p for p in self.check(self.claim)))

    def test_finding_where_the_math_is_fine_fails(self):
        self.claim["calc"]["expr"] = "(110-100)/100*100"
        self.claim["quote"] = "Revenue rose 10% to EUR 110 million"
        self.assertTrue(any("matches the stated" in p for p in self.check(self.claim)))

    def test_confirmed_with_wrong_math_fails(self):
        self.claim["verdict"] = "confirmed"
        self.assertTrue(any("marked confirmed" in p for p in self.check(self.claim)))

    def test_no_evidence_no_verdict(self):
        self.assertTrue(any("needs verified evidence" in p for p in self.check(
            {"id": "C2", "page": 1, "quote": "Direct sales were strong in Norway", "verdict": "wrong",
             "correction": "x"})))

    def test_unverified_needs_only_a_real_quote(self):
        self.assertEqual(self.check(
            {"id": "C2", "page": 1, "quote": "Direct sales were strong in Norway", "verdict": "unverified"}), [])

    def test_source_quote_is_matched_against_the_snapshot(self):
        url = "https://example.org/a"
        fc.source_path(self.work, url).write_text("Revenue in Norway was EUR 12 million.")
        claim = {"id": "C3", "page": 1, "quote": "Direct sales were strong in Norway", "verdict": "misleading",
                 "correction": "x", "evidence": [{"kind": "source", "url": url, "quote": "Revenue in Norway was EUR 12 million"}]}
        self.assertEqual(self.check(claim), [])
        claim["evidence"][0]["quote"] = "Revenue in Norway was EUR 90 million"
        self.assertTrue(self.check(claim))

    def test_coverage_reports_skipped_numbers(self):
        cov = fc.coverage([self.claim], [], self.doc)
        self.assertEqual(cov["covered"], 1)
        self.assertEqual(len(cov["untouched"]), 1)
        cov = fc.coverage([self.claim], [{"sentence": cov["untouched"][0]["id"], "reason": "restates p1"}], self.doc)
        self.assertEqual(cov["percent"], 100.0)

    def test_skip_without_reason_does_not_count(self):
        sid = fc.coverage([self.claim], [], self.doc)["untouched"][0]["id"]
        self.assertEqual(fc.coverage([self.claim], [{"sentence": sid}], self.doc)["covered"], 1)

    def test_failed_claim_is_downgraded(self):
        final = fc.apply_downgrades({"claims": [self.claim], "failed": {"C1": ["bad"]}})
        self.assertEqual(final[0]["verdict"], "unverified")
        self.assertEqual(final[0]["model_verdict"], "wrong")


if __name__ == "__main__":
    unittest.main()
