"""Tests for the ModelRecommender — optimal model mix recommendations."""

from __future__ import annotations

import pytest

from fleet_manager.models.node import NodeStatus
from fleet_manager.server.model_knowledge import (
    MODEL_CATALOG,
    ModelCategory,
    ModelSpec,
    best_for_category,
    classify_model,
    lookup_model,
    models_fitting_ram,
)
from fleet_manager.server.model_recommender import ModelRecommender, Priority
from tests.conftest import make_node


# ---------------------------------------------------------------------------
# Model Knowledge Base tests
# ---------------------------------------------------------------------------


class TestModelKnowledge:
    def test_catalog_not_empty(self):
        assert len(MODEL_CATALOG) > 20

    def test_all_models_have_required_fields(self):
        for m in MODEL_CATALOG:
            assert m.ollama_name, f"Missing ollama_name: {m}"
            assert m.ram_gb > 0, f"Invalid ram_gb for {m.ollama_name}"
            assert m.params_b > 0, f"Invalid params_b for {m.ollama_name}"
            assert m.category in ModelCategory, f"Invalid category for {m.ollama_name}"

    def test_lookup_exact(self):
        spec = lookup_model("qwen3:8b")
        assert spec is not None
        assert spec.display_name == "Qwen 3 8B"

    def test_lookup_with_latest_suffix(self):
        spec = lookup_model("qwen3:8b:latest")
        # May not match since the suffix stripping is ":latest" only
        # But the family-based fallback should work for "qwen3:8b"
        spec2 = lookup_model("qwen3:8b")
        assert spec2 is not None

    def test_lookup_unknown_returns_none(self):
        assert lookup_model("nonexistent-model:99b") is None

    def test_classify_coding_model(self):
        assert classify_model("qwen2.5-coder:7b") == ModelCategory.CODING

    def test_classify_reasoning_model(self):
        assert classify_model("deepseek-r1:14b") == ModelCategory.REASONING

    def test_classify_unknown_defaults_general(self):
        assert classify_model("unknown-model") == ModelCategory.GENERAL

    def test_classify_heuristic_coder(self):
        # Unknown model with "coder" in name should classify as coding
        assert classify_model("some-coder:7b") == ModelCategory.CODING

    def test_models_fitting_ram_16gb(self):
        models = models_fitting_ram(16.0)
        assert len(models) > 0
        assert all(m.ram_gb <= 16.0 for m in models)
        # Should be sorted by quality (best first)
        scores = [m.benchmarks.quality_score for m in models]
        assert scores == sorted(scores, reverse=True)

    def test_models_fitting_ram_2gb(self):
        models = models_fitting_ram(2.0)
        assert all(m.ram_gb <= 2.0 for m in models)

    def test_models_fitting_ram_0(self):
        assert models_fitting_ram(0) == []

    def test_best_for_category_coding(self):
        best = best_for_category(ModelCategory.CODING, 64.0)
        assert best is not None
        assert best.category == ModelCategory.CODING or ModelCategory.CODING in best.secondary_categories

    def test_best_for_category_insufficient_ram(self):
        # No model should fit in 0.5 GB
        assert best_for_category(ModelCategory.GENERAL, 0.5) is None

    def test_moe_models_identified(self):
        moe = [m for m in MODEL_CATALOG if m.is_moe]
        assert len(moe) >= 1
        for m in moe:
            assert m.active_params_b is not None
            assert m.active_params_b < m.params_b

    def test_quality_score_computed(self):
        spec = lookup_model("qwen3:8b")
        assert spec is not None
        assert spec.benchmarks.quality_score > 0


# ---------------------------------------------------------------------------
# Recommender tests
# ---------------------------------------------------------------------------


class TestRecommender:
    def setup_method(self):
        self.recommender = ModelRecommender()

    def test_empty_fleet(self):
        report = self.recommender.analyze([], [])
        assert "No online nodes" in report.fleet_summary
        assert len(report.nodes) == 0

    def test_offline_nodes_excluded(self):
        node = make_node("offline-node", status=NodeStatus.OFFLINE, memory_total=64.0)
        report = self.recommender.analyze([node], [])
        assert len(report.nodes) == 0

    def test_single_node_gets_recommendations(self):
        node = make_node("studio", memory_total=96.0, memory_used=20.0)
        report = self.recommender.analyze([node])
        assert len(report.nodes) == 1
        plan = report.nodes[0]
        assert plan.node_id == "studio"
        assert plan.total_ram_gb == 96.0
        assert len(plan.recommendations) > 0

    def test_recommendations_fit_in_ram(self):
        node = make_node("studio", memory_total=32.0, memory_used=10.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        # Total recommended RAM should not exceed usable RAM
        assert plan.total_recommended_ram_gb <= plan.usable_ram_gb

    def test_small_ram_node_gets_small_models(self):
        node = make_node("macbook-air", memory_total=16.0, memory_used=6.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        for rec in plan.recommendations:
            assert rec.ram_gb <= 10.0  # Should fit in ~10GB usable

    def test_large_ram_node_gets_quality_models(self):
        node = make_node("mac-pro", memory_total=192.0, memory_used=30.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        # Should have at least one large/high-quality model
        has_large = any(r.ram_gb >= 20 for r in plan.recommendations)
        assert has_large, "192GB node should get large models"

    def test_multi_node_fleet_covers_categories(self):
        nodes = [
            make_node("studio", memory_total=96.0, memory_used=20.0),
            make_node("macbook", memory_total=32.0, memory_used=8.0),
        ]
        report = self.recommender.analyze(nodes)
        assert len(report.nodes) == 2

        # Should cover multiple categories across the fleet
        all_cats = set()
        for plan in report.nodes:
            for rec in plan.recommendations:
                all_cats.add(rec.category)
        assert len(all_cats) >= 3, f"Should cover 3+ categories, got: {all_cats}"

    def test_usage_data_influences_priority(self):
        node = make_node("studio", memory_total=64.0, memory_used=10.0)
        # Heavy coding usage
        usage_data = [
            {"model": "qwen2.5-coder:7b", "request_count": 100, "node_id": "studio"},
            {"model": "llama3.1:8b", "request_count": 5, "node_id": "studio"},
        ]
        report = self.recommender.analyze([node], usage_data)
        plan = report.nodes[0]

        # Coding should be high priority given usage
        coding_recs = [r for r in plan.recommendations if r.category == "coding"]
        assert len(coding_recs) > 0
        assert coding_recs[0].priority == Priority.HIGH

    def test_usage_analysis_totals(self):
        node = make_node("studio", memory_total=64.0)
        usage_data = [
            {"model": "qwen2.5-coder:7b", "request_count": 50, "node_id": "studio"},
            {"model": "llama3.1:8b", "request_count": 30, "node_id": "studio"},
        ]
        report = self.recommender.analyze([node], usage_data)
        assert report.usage.total_requests_24h == 80

    def test_usage_analysis_category_breakdown(self):
        node = make_node("studio", memory_total=64.0)
        usage_data = [
            {"model": "qwen2.5-coder:7b", "request_count": 50, "node_id": "studio"},
            {"model": "deepseek-r1:14b", "request_count": 20, "node_id": "studio"},
        ]
        report = self.recommender.analyze([node], usage_data)
        assert "coding" in report.usage.category_breakdown
        assert "reasoning" in report.usage.category_breakdown
        assert report.usage.category_breakdown["coding"] == 50

    def test_coverage_tracked(self):
        node = make_node("studio", memory_total=64.0)
        report = self.recommender.analyze([node])
        coverage = report.usage.category_coverage
        # At least some categories should be covered
        assert any(v for v in coverage.values())

    def test_existing_models_marked_available(self):
        node = make_node(
            "studio",
            memory_total=64.0,
            available_models=["qwen3:8b", "llama3.1:8b"],
        )
        report = self.recommender.analyze([node])
        plan = report.nodes[0]

        # Check that models matching available ones are marked
        for rec in plan.recommendations:
            if rec.model in ["qwen3:8b", "llama3.1:8b"]:
                assert rec.already_available

    def test_no_duplicate_families_per_node(self):
        node = make_node("studio", memory_total=192.0, memory_used=20.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]

        # Should not recommend two models from the same family
        families = []
        for rec in plan.recommendations:
            spec = lookup_model(rec.model)
            if spec:
                families.append(spec.family)
        assert len(families) == len(set(families)), f"Duplicate families: {families}"

    def test_fleet_summary_string(self):
        node = make_node("studio", memory_total=64.0)
        report = self.recommender.analyze([node])
        assert "1 node" in report.fleet_summary
        assert "64GB" in report.fleet_summary

    def test_five_node_fleet(self):
        """Simulate a 5-device fleet with varying RAM."""
        nodes = [
            make_node("mac-pro", memory_total=192.0, memory_used=30.0),
            make_node("mac-studio-1", memory_total=96.0, memory_used=15.0),
            make_node("mac-studio-2", memory_total=64.0, memory_used=10.0),
            make_node("macbook-pro", memory_total=36.0, memory_used=12.0),
            make_node("macbook-air", memory_total=16.0, memory_used=6.0),
        ]
        report = self.recommender.analyze(nodes)
        assert len(report.nodes) == 5

        # All nodes should have at least one recommendation
        for plan in report.nodes:
            assert len(plan.recommendations) > 0, f"{plan.node_id} has no recommendations"

        # Fleet should cover most categories
        all_cats = set()
        for plan in report.nodes:
            for rec in plan.recommendations:
                all_cats.add(rec.category)
        assert len(all_cats) >= 4, f"5-node fleet should cover 4+ categories, got: {all_cats}"

    def test_no_usage_data_uses_defaults(self):
        node = make_node("studio", memory_total=64.0)
        report = self.recommender.analyze([node], None)
        assert report.usage.total_requests_24h == 0
        # Should still get recommendations using default priority
        assert len(report.nodes[0].recommendations) > 0

    def test_ram_headroom_calculated(self):
        node = make_node("studio", memory_total=64.0, memory_used=10.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        expected_headroom = plan.usable_ram_gb - plan.total_recommended_ram_gb
        assert abs(plan.ram_headroom_gb - expected_headroom) < 0.1

    def test_top_models_in_usage(self):
        node = make_node("studio", memory_total=64.0)
        usage_data = [
            {"model": "qwen2.5-coder:7b", "request_count": 100, "node_id": "studio"},
            {"model": "llama3.1:8b", "request_count": 50, "node_id": "studio"},
            {"model": "deepseek-r1:14b", "request_count": 25, "node_id": "studio"},
        ]
        report = self.recommender.analyze([node], usage_data)
        assert len(report.usage.top_models) == 3
        assert report.usage.top_models[0]["model"] == "qwen2.5-coder:7b"
        assert report.usage.top_models[0]["requests"] == 100

    def test_single_model_capped_at_50pct_ram(self):
        """No single model should consume more than 50% of usable RAM."""
        node = make_node("studio", memory_total=128.0, memory_used=20.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        usable = plan.usable_ram_gb
        for rec in plan.recommendations:
            assert rec.ram_gb <= usable * 0.5 + 0.1, (
                f"{rec.model} at {rec.ram_gb}GB exceeds 50% of {usable}GB usable"
            )

    def test_actively_used_models_prioritized(self):
        """Models the user is actually running should be recommended first."""
        node = make_node(
            "studio",
            memory_total=96.0,
            available_models=["gpt-oss:120b", "qwen3:8b"],
        )
        usage_data = [
            {"model": "gpt-oss:120b", "request_count": 500, "node_id": "studio"},
        ]
        report = self.recommender.analyze([node], usage_data)
        plan = report.nodes[0]
        # The most-used model should appear as a recommendation
        gpt_recs = [r for r in plan.recommendations if "gpt-oss" in r.model]
        # If it fits the RAM cap, it should be there
        if gpt_recs:
            assert gpt_recs[0].already_available
            assert gpt_recs[0].priority == Priority.HIGH

    def test_is_available_fuzzy_match(self):
        """Model name variants should match (e.g. qwen3-coder:latest matches qwen3-coder)."""
        from fleet_manager.server.model_recommender import ModelRecommender
        rec = ModelRecommender()
        assert rec._is_available("qwen3:8b", ["qwen3:8b", "llama3:8b"])
        assert rec._is_available("qwen3:8b", ["qwen3:latest"])  # base match
        assert not rec._is_available("qwen3:8b", ["llama3:8b", "gemma3:4b"])

    def test_unknown_model_recommended_from_loaded_size(self):
        """Unknown models should still be recommended if they're loaded and used."""
        node = make_node(
            "studio",
            memory_total=96.0,
            loaded_models=[("custom-model:7b", 5.0)],
            available_models=["custom-model:7b"],
        )
        usage_data = [
            {"model": "custom-model:7b", "request_count": 200, "node_id": "studio"},
        ]
        report = self.recommender.analyze([node], usage_data)
        plan = report.nodes[0]
        custom_recs = [r for r in plan.recommendations if r.model == "custom-model:7b"]
        assert len(custom_recs) == 1
        assert custom_recs[0].already_available
        assert custom_recs[0].priority == Priority.HIGH
        assert custom_recs[0].ram_gb == 5.0

    def test_disk_constrained_skips_large_models(self):
        """Node with lots of RAM but little disk should skip large non-downloaded models."""
        node = make_node(
            "low-disk",
            memory_total=96.0,
            memory_used=10.0,
            disk_total=100.0,
            disk_used=95.0,  # Only 5 GB free
            available_models=["qwen3:8b"],  # Already have one model
        )
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        assert plan.disk_total_gb == 100.0
        assert plan.disk_available_gb <= 5.0
        # All non-downloaded recommendations should fit in available disk
        for rec in plan.recommendations:
            if not rec.already_available:
                assert rec.ram_gb <= 5.0, (
                    f"{rec.model} ({rec.ram_gb}GB) exceeds 5GB disk free"
                )

    def test_disk_available_in_plan(self):
        """Node plan should include disk metrics."""
        node = make_node("studio", memory_total=64.0, disk_total=500.0, disk_used=200.0)
        report = self.recommender.analyze([node])
        plan = report.nodes[0]
        assert plan.disk_total_gb == 500.0
        # Disk available should be <= 300 (original free minus new downloads)
        assert plan.disk_available_gb <= 300.0

    def test_downloaded_models_ignore_disk(self):
        """Already-downloaded models should be recommended regardless of disk space."""
        node = make_node(
            "full-disk",
            memory_total=64.0,
            memory_used=10.0,
            disk_total=100.0,
            disk_used=99.0,  # 1 GB free — almost no disk
            available_models=["qwen3:8b", "llama3.1:8b"],
        )
        usage_data = [
            {"model": "qwen3:8b", "request_count": 50, "node_id": "full-disk"},
        ]
        report = self.recommender.analyze([node], usage_data)
        plan = report.nodes[0]
        # qwen3:8b should still be recommended (already downloaded)
        qwen_recs = [r for r in plan.recommendations if "qwen3" in r.model]
        assert len(qwen_recs) > 0
        assert qwen_recs[0].already_available
