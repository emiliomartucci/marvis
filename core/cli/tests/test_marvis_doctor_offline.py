from __future__ import annotations

import pytest

from core.cli import marvis_doctor


def test_offline_connectivity_check_never_opens_a_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline doctor attempted a network connection")

    monkeypatch.setattr(marvis_doctor.socket, "create_connection", fail_if_called)

    result = marvis_doctor._check_connectivity(offline=True)

    assert result.level == "ok"
    assert result.detail == "skipped (--offline)"


def test_offline_empty_model_cache_reports_keyword_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty-hf-cache"))

    results = marvis_doctor._check_granite_model(offline=True)

    cache = next(result for result in results if result.name == "granite_model_cache")
    assert cache.level == "warning"
    assert "keyword fallback" in cache.detail
    assert "fetched automatically" not in cache.detail


def test_doctor_accepts_exact_revision_in_runtime_cache_layout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hf_home = tmp_path / "hf-cache"
    model_dir = "models--" + marvis_doctor.GRANITE_MODEL_ID.replace("/", "--")
    snapshot = (
        hf_home
        / model_dir
        / "snapshots"
        / marvis_doctor.GRANITE_MODEL_REVISION
    )
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "onnx" / "model.onnx").write_bytes(b"model")
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(hf_home))

    results = marvis_doctor._check_granite_model(offline=True)

    cache = next(result for result in results if result.name == "granite_model_cache")
    assert cache.level == "ok"
    assert str(snapshot) in cache.detail


def test_doctor_rejects_unpinned_snapshot_and_pins_provisioning_command(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hf_home = tmp_path / "hf-cache"
    model_dir = "models--" + marvis_doctor.GRANITE_MODEL_ID.replace("/", "--")
    snapshot = hf_home / "hub" / model_dir / "snapshots" / "wrong-revision"
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "onnx" / "model.onnx").write_bytes(b"model")
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(hf_home))

    results = marvis_doctor._check_granite_model(offline=True)

    cache = next(result for result in results if result.name == "granite_model_cache")
    assert cache.level == "warning"
    assert "keyword fallback" in cache.detail
    assert f"revision='{marvis_doctor.GRANITE_MODEL_REVISION}'" in cache.fix
    assert "cache_dir=" in cache.fix
