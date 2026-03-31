"""Tests for DiffusionKit image generation backend."""

import pytest

from fleet_manager.node.image_server import _MODEL_BINARIES, _is_diffusionkit, _resolve_binary


class TestDiffusionKitModels:
    """Tests for DiffusionKit model registration."""

    def test_sd3_medium_in_model_binaries(self):
        assert "sd3-medium" in _MODEL_BINARIES

    def test_sd3_5_large_in_model_binaries(self):
        assert "sd3.5-large" in _MODEL_BINARIES

    def test_sd3_medium_uses_diffusionkit(self):
        cmd = _MODEL_BINARIES["sd3-medium"]
        assert cmd[0] == "diffusionkit-cli"
        assert "--model-version" in cmd
        assert "argmaxinc/mlx-stable-diffusion-3-medium" in cmd

    def test_sd3_5_large_uses_t5(self):
        cmd = _MODEL_BINARIES["sd3.5-large"]
        assert cmd[0] == "diffusionkit-cli"
        assert "--t5" in cmd
        assert "argmaxinc/mlx-stable-diffusion-3.5-large" in cmd

    def test_mflux_models_still_present(self):
        assert "z-image-turbo" in _MODEL_BINARIES
        assert "flux-dev" in _MODEL_BINARIES
        assert "flux-schnell" in _MODEL_BINARIES


class TestIsDiffusionKit:
    """Tests for backend detection."""

    def test_diffusionkit_detected(self):
        assert _is_diffusionkit(["diffusionkit-cli", "--model-version", "foo"]) is True

    def test_mflux_not_diffusionkit(self):
        assert _is_diffusionkit(["mflux-generate-z-image-turbo"]) is False

    def test_mflux_generate_not_diffusionkit(self):
        assert _is_diffusionkit(["mflux-generate", "--model", "dev"]) is False


class TestResolveBinary:
    """Tests for _resolve_binary with DiffusionKit models."""

    def test_resolve_sd3_when_installed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
        result = _resolve_binary("sd3-medium")
        assert result is not None
        assert result[0] == "diffusionkit-cli"

    def test_resolve_sd3_when_not_installed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: None)
        result = _resolve_binary("sd3-medium")
        assert result is None

    def test_resolve_mflux_still_works(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
        result = _resolve_binary("z-image-turbo")
        assert result is not None
        assert result[0] == "mflux-generate-z-image-turbo"


class TestDiffusionKitDetection:
    """Tests for DiffusionKit detection in the collector."""

    def test_detect_diffusionkit_models(self, monkeypatch):
        from fleet_manager.node.collector import _detect_image_models

        def fake_which(name):
            if name == "diffusionkit-cli":
                return "/usr/local/bin/diffusionkit-cli"
            return None

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setattr(
            "fleet_manager.node.collector.psutil",
            type("FakePsutil", (), {"process_iter": staticmethod(lambda attrs: [])}),
            raising=False,
        )
        result = _detect_image_models()
        assert result is not None
        model_names = [m.name for m in result.models_available]
        assert "sd3-medium" in model_names
        assert "sd3.5-large" in model_names

    def test_detect_both_mflux_and_diffusionkit(self, monkeypatch):
        from fleet_manager.node.collector import _detect_image_models

        def fake_which(name):
            # Both mflux and DiffusionKit installed
            if name in ("mflux-generate-z-image-turbo", "diffusionkit-cli"):
                return f"/usr/local/bin/{name}"
            return None

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setattr(
            "fleet_manager.node.collector.psutil",
            type("FakePsutil", (), {"process_iter": staticmethod(lambda attrs: [])}),
            raising=False,
        )
        result = _detect_image_models()
        assert result is not None
        model_names = [m.name for m in result.models_available]
        assert "z-image-turbo" in model_names
        assert "sd3-medium" in model_names
        assert "sd3.5-large" in model_names

    def test_detect_neither_returns_none(self, monkeypatch):
        from fleet_manager.node.collector import _detect_image_models

        monkeypatch.setattr("shutil.which", lambda _: None)
        result = _detect_image_models()
        assert result is None


class TestDiffusionKitModelKnowledge:
    """Tests for SD3 models in the knowledge catalog."""

    def test_sd3_medium_in_catalog(self):
        from fleet_manager.server.model_knowledge import ModelCategory, lookup_model

        spec = lookup_model("sd3-medium")
        assert spec is not None
        assert spec.category == ModelCategory.IMAGE
        assert spec.params_b == 2.0

    def test_sd3_5_large_in_catalog(self):
        from fleet_manager.server.model_knowledge import ModelCategory, lookup_model

        spec = lookup_model("sd3.5-large")
        assert spec is not None
        assert spec.category == ModelCategory.IMAGE
        assert spec.params_b == 8.0
        assert spec.ram_gb == 16.0
