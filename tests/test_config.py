from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from adw.config import AdwConfig, load_config


def test_defaults() -> None:
    config = AdwConfig()
    assert config.workflow.max_fix_iterations == 3
    assert config.workflow.max_review_iterations == 2
    assert config.agents.default.backend == "claude-code"
    assert config.backends.for_backend("claude-code").binary == "claude"
    assert config.backends.for_backend("codex").binary == "codex"
    assert config.backends.for_backend("opencode").binary == "opencode"


def test_gate_order_defaults_to_declaration_order() -> None:
    config = AdwConfig.model_validate(
        {"gates": {"b": {"command": "true"}, "a": {"command": "true"}}}
    )
    assert config.gate_order() == ["b", "a"]


def test_resolve_role_falls_back_to_default() -> None:
    config = AdwConfig.model_validate(
        {
            "agents": {
                "default": {"backend": "claude-code", "model": "sonnet"},
                "roles": {"plan": {"backend": "opencode", "model": "anthropic/claude-opus-4"}},
            }
        }
    )
    assert config.resolve_role("plan").backend == "opencode"
    assert config.resolve_role("build").backend == "claude-code"
    assert config.resolve_role("build").model == "sonnet"


def test_typo_rejected() -> None:
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"gatez": {}})


def test_queue_file_failures_defaults_off() -> None:
    assert AdwConfig.model_validate({}).queue.file_failures is False


def test_queue_file_failures_enabled() -> None:
    config = AdwConfig.model_validate({"queue": {"file_failures": True}})
    assert config.queue.file_failures is True


def test_queue_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"queue": {"bogus": True}})


def test_limits_parse_and_default() -> None:
    config = AdwConfig.model_validate({"limits": {"max_cost_usd": 2.5}})
    assert config.limits.max_cost_usd == 2.5
    assert AdwConfig.model_validate({}).limits.max_cost_usd is None
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"limits": {"bogus": 1}})


def test_model_router_defaults() -> None:
    router = AdwConfig().model_router
    assert router.enabled is False
    assert router.ladders == {"claude-code": ["opus", "sonnet", "haiku"]}
    assert router.downshift_warn == 0.80
    assert router.downshift_critical == 0.95
    assert router.escalate_after == 2


def test_model_router_parse() -> None:
    config = AdwConfig.model_validate(
        {
            "model_router": {
                "enabled": True,
                "ladders": {"claude-code": ["fable", "opus", "sonnet", "haiku"]},
                "downshift_warn": 0.5,
                "escalate_after": 1,
            }
        }
    )
    assert config.model_router.enabled is True
    assert config.model_router.ladders == {"claude-code": ["fable", "opus", "sonnet", "haiku"]}
    assert config.model_router.downshift_warn == 0.5
    assert config.model_router.escalate_after == 1


def test_model_router_empty_ladder_rejected() -> None:
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"model_router": {"ladders": {"claude-code": []}}})


def test_model_router_bad_thresholds_rejected() -> None:
    with pytest.raises(ValidationError):
        AdwConfig.model_validate(
            {"model_router": {"downshift_warn": 0.9, "downshift_critical": 0.8}}
        )


def test_fusion_defaults() -> None:
    config = AdwConfig.model_validate({"fusion": {}})
    assert config.fusion.opinions == ["opinion_a", "opinion_b"]
    assert config.fusion.max_validate_iterations == 3
    assert config.fusion.validate_timeout == 300


def test_fusion_empty_opinions_rejected() -> None:
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"fusion": {"opinions": []}})


def test_fusion_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"fusion": {"bogus": 1}})


def test_notify_defaults_off() -> None:
    config = AdwConfig()
    assert config.notify.macos is False
    assert config.notify.webhook is None


def test_notify_parses_and_rejects_typos() -> None:
    config = AdwConfig.model_validate(
        {"notify": {"macos": True, "webhook": "https://x"}}
    )
    assert config.notify.macos is True
    assert config.notify.webhook == "https://x"
    with pytest.raises(ValidationError):
        AdwConfig.model_validate({"notify": {"webook": "https://x"}})


def test_merge_precedence(tmp_path: Path) -> None:
    global_path = tmp_path / "global.yaml"
    global_path.write_text(
        "agents:\n  default: {backend: opencode, model: global-model}\n"
        "workflow: {max_fix_iterations: 7}\n"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "adw.yaml").write_text("agents:\n  default: {backend: opencode, model: repo-model}\n")
    config = load_config(repo, global_path=global_path)
    # repo layer wins where set; global survives where repo is silent
    assert config.agents.default.model == "repo-model"
    assert config.workflow.max_fix_iterations == 7
    # CLI overrides win over everything
    config = load_config(
        repo,
        overrides={"workflow": {"max_fix_iterations": 1}},
        global_path=global_path,
    )
    assert config.workflow.max_fix_iterations == 1


def test_backend_alias_hyphen(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "adw.yaml").write_text("backends:\n  claude-code: {binary: /opt/claude}\n")
    config = load_config(repo)
    assert config.backends.for_backend("claude-code").binary == "/opt/claude"
