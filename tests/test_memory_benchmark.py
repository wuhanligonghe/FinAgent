"""Dataset/scorer contracts using explicit fake outputs, not model quality scores."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from finagent import memory_benchmark as b
from scripts.build_memory_benchmark import write_dataset


SOURCE = Path("evaluation/memory_v1")


class MemoryBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases, self.memories, self.gold, self.protocol = b.load_dataset(SOURCE)

    def write_jsonl(self, path, values):
        path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")
        return path

    def case(self, category):
        return next(c for c in self.cases if c["split"] == "holdout" and c["category"] == category)

    def prediction(self, case, ids=(), answer="测试夹具答复"):
        core = [key for key in ids if self.memories[key]["core"]]
        return dict(id=case["id"], dataset_manifest_sha256=b.digest(SOURCE / "manifest.json"),
                    memory_enabled=True, retrieved_ids=[key for key in ids if key not in core],
                    core_ids=core, injected_ids=list(ids), status="completed", answer=answer)

    def review(self, prediction, *, passed=False, claims=(), invalid=()):
        return dict(reviewer="synthetic_unit_test_fixture", reviewed_at="2026-11-05T11:00:00+08:00",
            independent_of_authoring=False, judgments=[dict(id=prediction["id"],
                answer_sha256=b.answer_digest(prediction["answer"]), complete=True,
                requirements=[dict(id=r["id"], passed=passed, evidence_quote=prediction["answer"] if passed else "")
                              for r in self.gold[prediction["id"]]["requirements"]],
                memory_claims=list(claims), invalid_uses=list(invalid))])

    def run_score(self, prediction, review=None):
        predictions = self.write_jsonl(self.root / "predictions.jsonl", [prediction])
        review_path = self.root / "review.json" if review else None
        if review:
            review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        return b.score(SOURCE, predictions, split="holdout", review_path=review_path)

    def test_dataset_size_categories_scope_splits_and_opaque_ids(self):
        result = b.validate(SOURCE)
        self.assertEqual(result["cases"], 48)
        self.assertEqual(result["memories"], 364)
        self.assertEqual(result["splits"], {"development": 24, "holdout": 24})
        self.assertEqual(set(result["categories"]), set(b.CATEGORIES))
        self.assertTrue(all(count == 4 for count in result["categories"].values()))
        for split in ("development", "holdout"):
            self.assertEqual({c["category"] for c in self.cases if c["split"] == split}, set(b.CATEGORIES))
        dev_users = {c["scope"]["user_id"] for c in self.cases if c["split"] == "development"}
        held_users = {c["scope"]["user_id"] for c in self.cases if c["split"] == "holdout"}
        self.assertFalse(dev_users & held_users)
        self.assertTrue(all(m["id"].startswith("mem_") and len(m["id"]) == 36 for m in self.memories.values()))

    def test_rebuilding_is_deterministic_and_has_no_model_calls(self):
        write_dataset(self.root / "first")
        write_dataset(self.root / "second")
        for filename in ("inputs.jsonl", "memories.jsonl", "gold.jsonl", "manifest.json"):
            self.assertEqual((self.root / "first" / filename).read_bytes(), (self.root / "second" / filename).read_bytes())
        self.assertFalse(self.protocol["labels_visible_to_generator"])
        for row in b.rows(SOURCE / "inputs.jsonl", b.Input):
            self.assertNotIn("relevance", row)
            self.assertNotIn("requirements", row)

    def test_frozen_files_detect_tampering(self):
        target = self.root / "tampered"
        shutil.copytree(SOURCE, target)
        with (target / "inputs.jsonl").open("a") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(ValueError, "摘要不匹配"):
            b.validate(target)

    def test_invalid_positive_scope_and_delete_labels_are_rejected(self):
        for category in ("user_isolation", "project_isolation", "deleted_memory"):
            with self.subTest(category=category):
                target = self.root / category
                shutil.copytree(SOURCE, target)
                case = self.case(category)
                labels = b.rows(target / "gold.jsonl", b.Gold)
                label = next(g for g in labels if g["id"] == case["id"])
                bad = label["invalid_use_rules"][0]["memory_ids"][0]
                next(g for g in label["relevance"] if g["memory_id"] == bad)["grade"] = 3
                label["memory_dependency"] = "required"
                self.write_jsonl(target / "gold.jsonl", labels)
                with self.assertRaisesRegex(ValueError, "不能标为正相关"):
                    b.load_dataset(target, verify=False)

    def test_metric_formula_and_empty_gold_do_not_grant_free_perfect_scores(self):
        relevance = {"a": 3, "b": 2, "c": 1, "noise": 0}
        result = b.retrieval_metrics(["noise", "c", "a", "b"], relevance, 3)
        self.assertAlmostEqual(result["precision"], 2 / 3)
        self.assertAlmostEqual(result["recall"], 2 / 3)
        self.assertAlmostEqual(result["mrr"], .5)
        self.assertGreater(result["ndcg"], 0)
        self.assertLess(result["ndcg"], 1)
        ideal = b.retrieval_metrics(["a", "b", "c"], relevance, 3)
        self.assertAlmostEqual(ideal["ndcg"], 1)
        self.assertAlmostEqual(b.retrieval_metrics(["b", "a"], {"a": 3, "b": 1}, 2)["ndcg"], .7098097413968655)
        empty = b.retrieval_metrics([], {}, 5)
        self.assertTrue(all(v is None for v in empty.values()))
        missing = b.retrieval_metrics([], {"a": 3}, 5)
        self.assertTrue(all(v == 0 for v in missing.values()))
        self.assertEqual(b.retrieval_metrics(["noise"], {"noise": 0}, 5)["precision"], 0)

    def test_missing_cases_stay_in_recall_denominator_generation_not_faked(self):
        case = self.case("semantic_recall")
        relevant = next(g["memory_id"] for g in self.gold[case["id"]]["relevance"] if g["grade"])
        report = self.run_score(self.prediction(case, [relevant]))
        self.assertEqual(report["planned"], 24)
        self.assertEqual(report["predicted"], 1)
        self.assertEqual(len(report["missing"]), 23)
        self.assertLess(report["retrieval"]["5"]["recall"], 1)
        self.assertEqual(report["generation"]["status"], "not_evaluated")
        self.assertIsNone(report["generation"]["memory_claim_support_rate"])
        self.assertIsNone(report["generation"]["invalid_memory_adoption_rate"])
        self.assertFalse(report["independent_acceptance_passed"])

    def test_core_is_separate_from_ranking_but_required_for_generation_coverage(self):
        case = self.case("core_residency")
        core = next(g["memory_id"] for g in self.gold[case["id"]]["relevance"] if g["grade"])
        prediction = self.prediction(case, [core])
        review = self.review(prediction, passed=True)
        report = self.run_score(prediction, review)
        row = next(r for r in report["retrieval_records"] if r["id"] == case["id"])
        self.assertIsNone(row["at_k"]["5"]["recall"])
        self.assertEqual(report["generation"]["required_information_coverage"], 1)
        self.assertEqual(report["generation"]["status"], "partial")
        prediction["core_ids"] = prediction["injected_ids"] = []
        with self.assertRaisesRegex(ValueError, "未实际注入"):
            self.run_score(prediction, review)

    def test_actual_injected_bad_memory_adoption_is_measured(self):
        case = self.case("user_isolation")
        rule = self.gold[case["id"]]["invalid_use_rules"][0]
        bad = rule["memory_ids"][0]
        prediction = self.prediction(case, [bad], "你的默认金额单位已经确定。")
        claims = [dict(quote=prediction["answer"], verdict="contradicted", memory_ids=[bad])]
        invalid = [dict(rule_id=rule["id"], memory_id=bad, quote=prediction["answer"])]
        report = self.run_score(prediction, self.review(prediction, claims=claims, invalid=invalid))
        self.assertEqual(report["generation"]["memory_claim_support_rate"], 0)
        self.assertEqual(report["generation"]["required_information_coverage"], 0)
        self.assertEqual(report["generation"]["invalid_memory_adoption_rate"], 1)
        self.assertEqual(report["empty_gold_false_retrieval_rate"], 1)

    def test_invalid_review_cannot_invent_quotes_sources_or_use(self):
        case = self.case("semantic_recall")
        target = next(g["memory_id"] for g in self.gold[case["id"]]["relevance"] if g["grade"])
        prediction = self.prediction(case, [target])
        review = self.review(prediction, passed=True)
        review["judgments"][0]["answer_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "答案摘要不匹配"):
            self.run_score(prediction, review)
        review = self.review(prediction, passed=True)
        review["judgments"][0]["requirements"][0]["evidence_quote"] = "不存在的答复引文"
        with self.assertRaisesRegex(ValueError, "引文不在"):
            self.run_score(prediction, review)
        review = self.review(prediction, passed=True)
        review["judgments"][0]["complete"] = False
        with self.assertRaisesRegex(ValueError, "审核未完成"):
            self.run_score(prediction, review)

    def test_memory_disabled_or_wrong_split_is_not_a_valid_memory_run(self):
        prediction = self.prediction(self.case("semantic_recall"))
        prediction["memory_enabled"] = False
        with self.assertRaisesRegex(ValueError, "必须实际启用记忆"):
            self.run_score(prediction)
        prediction = self.prediction(next(c for c in self.cases if c["split"] == "development"))
        with self.assertRaisesRegex(ValueError, "不属于所选集合"):
            self.run_score(prediction)

    def test_predictions_are_bound_to_frozen_dataset_version(self):
        prediction = self.prediction(self.case("semantic_recall"))
        prediction["dataset_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "数据集摘要不匹配"):
            self.run_score(prediction)

    def test_retrieval_duplicates_and_fabricated_injection_are_rejected(self):
        case = self.case("semantic_recall")
        key = case["corpus_ids"][0]
        prediction = self.prediction(case, [key])
        prediction["retrieved_ids"] *= 2
        with self.assertRaisesRegex(ValueError, "重复"):
            self.run_score(prediction)
        prediction = self.prediction(case)
        prediction["injected_ids"] = [key]
        with self.assertRaisesRegex(ValueError, "不属于实际检索"):
            self.run_score(prediction)

    def test_historical_labels_allow_both_versions_and_conditional_misuse(self):
        case = self.case("historical_question")
        label = self.gold[case["id"]]
        positive = {g["memory_id"] for g in label["relevance"] if g["grade"]}
        self.assertEqual(len(positive), 2)
        self.assertEqual(set(label["invalid_use_rules"][0]["memory_ids"]), positive)
        self.assertEqual(len(label["requirements"]), 2)


if __name__ == "__main__":
    unittest.main()
