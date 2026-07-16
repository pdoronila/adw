"""Tests for `adw init` and the scaffold detection/rendering logic."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from adw.cli import app
from adw.config import GateConfig, load_config
from adw.scaffold import ProjectProfile, render_config, validate_rendered

runner = CliRunner()


def test_init_python_uv_project(target_repo: Path) -> None:
    (target_repo / "pyproject.toml").write_text(
        """
[project]
name = "demo"
version = "0.1.0"

[dependency-groups]
dev = ["ruff>=0.5", "mypy>=1.0", "pytest>=8"]
"""
    )
    (target_repo / "uv.lock").write_text("")

    result = runner.invoke(app, ["init", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    assert (target_repo / "adw.yaml").exists()

    config = load_config(target_repo)
    assert config.gate_order() == ["lint", "typecheck", "test"]
    for gate in config.gates.values():
        assert gate.command.startswith("uv run ")


def test_init_python_without_uv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
version = "0.1.0"
dependencies = ["pytest>=8"]
"""
    )

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates["test"].command == "pytest -x -q"
    assert "not a git repo" in result.output


def test_init_node_project(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"lint": "eslint .", "test": "vitest"}}'
    )
    (tmp_path / "pnpm-lock.yaml").write_text("")

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates["lint"].command == "pnpm run lint"
    assert config.gates["test"].command == "pnpm test"


def test_init_rust_project(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n')

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates["lint"].command == "cargo clippy -- -D warnings"
    assert config.gates["test"].command == "cargo test"


def test_init_swift_package(tmp_path: Path) -> None:
    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")
    (tmp_path / ".swiftlint.yml").write_text("")

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates["test"].command == "swift test"
    assert config.gates["lint"].command == "swiftlint"


def test_init_swift_package_without_swiftlint(tmp_path: Path) -> None:
    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert "lint" not in config.gates


def test_init_ios_project(tmp_path: Path) -> None:
    (tmp_path / "Demo.xcodeproj").mkdir()

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    command = config.gates["test"].command
    assert "xcodebuild test" in command
    assert "-project Demo.xcodeproj" in command
    assert "-scheme Demo" in command
    assert "scheme and simulator destination were guessed" in result.output


def test_init_ios_prefers_workspace(tmp_path: Path) -> None:
    (tmp_path / "Demo.xcodeproj").mkdir()
    (tmp_path / "Demo.xcworkspace").mkdir()

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert "-workspace Demo.xcworkspace" in config.gates["test"].command


def test_init_ios_beats_swift_package(tmp_path: Path) -> None:
    (tmp_path / "Demo.xcodeproj").mkdir()
    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert "xcodebuild" in config.gates["test"].command


def test_init_elixir_project(tmp_path: Path) -> None:
    (tmp_path / "mix.exs").write_text(
        """
defmodule Demo.MixProject do
  use Mix.Project

  defp deps do
    [
      {:credo, "~> 1.7", only: :dev},
      {:dialyxir, "~> 1.4", only: :dev}
    ]
  end
end
"""
    )

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates["lint"].command == "mix credo"
    assert config.gates["typecheck"].command == "mix dialyzer"
    assert config.gates["test"].command == "mix test"
    assert config.gate_order() == ["lint", "typecheck", "test"]


def test_init_elixir_minimal(tmp_path: Path) -> None:
    (tmp_path / "mix.exs").write_text(
        """
defmodule Demo.MixProject do
  use Mix.Project

  defp deps, do: []
end
"""
    )

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates["lint"].command == "mix format --check-formatted"
    assert "typecheck" not in config.gates


def test_init_refuses_overwrite(target_repo: Path) -> None:
    target = target_repo / "adw.yaml"
    target.write_text("original\n")

    result = runner.invoke(app, ["init", "--repo", str(target_repo)])
    assert result.exit_code == 1
    assert target.read_text() == "original\n"


def test_init_force_overwrites(target_repo: Path) -> None:
    target = target_repo / "adw.yaml"
    target.write_text("original\n")

    result = runner.invoke(app, ["init", "--repo", str(target_repo), "--force"])
    assert result.exit_code == 0, result.output
    assert target.read_text() != "original\n"


def test_init_unknown_project(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.gates == {}
    assert "could not detect" in result.output


def test_generated_yaml_is_valid_config() -> None:
    profiles = [
        ProjectProfile(
            ecosystem="python",
            gates={
                "lint": GateConfig(command="uv run ruff check .", timeout=120),
                "typecheck": GateConfig(command="uv run mypy", timeout=300),
                "test": GateConfig(command="uv run pytest -x -q", timeout=900),
            },
            notes=[],
        ),
        ProjectProfile(
            ecosystem="rust",
            gates={
                "lint": GateConfig(command="cargo clippy -- -D warnings", timeout=120),
                "test": GateConfig(command="cargo test", timeout=900),
            },
            notes=[],
        ),
        ProjectProfile(
            ecosystem="ios",
            gates={
                "test": GateConfig(
                    command=(
                        "xcodebuild test -project Demo.xcodeproj -scheme Demo "
                        "-destination 'platform=iOS Simulator,name=iPhone 16' -quiet"
                    ),
                    timeout=1800,
                ),
            },
            notes=[],
        ),
        ProjectProfile(ecosystem="unknown", gates={}, notes=["nope"]),
    ]
    for profile in profiles:
        config = validate_rendered(render_config(profile, "claude-code"))
        assert config.gate_order() == [
            name for name in ("lint", "typecheck", "test") if name in profile.gates
        ]
