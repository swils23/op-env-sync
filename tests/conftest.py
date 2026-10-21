import json
import sys
from pathlib import Path

import pytest

import openv

FAKE_OP = Path(__file__).with_name("fake_op.py")


class FakeOp:
    def __init__(self, root: Path):
        self.root = root

    def item(self, title: str, account: str = "my.1password.com", vault: str = "Private") -> dict | None:
        path = self.root / f"{account}__{vault}__{title}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def values(self, title: str, **where: str) -> dict[tuple[str, str], str]:
        return openv.remote_values(self.item(title, **where))

    def calls(self) -> list[list[str]]:
        log = self.root / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def fake_op(tmp_path, monkeypatch) -> FakeOp:
    root = tmp_path / "op"
    bin_dir = tmp_path / "bin"
    root.mkdir()
    bin_dir.mkdir()
    shim = bin_dir / "op"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_OP}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=":")
    monkeypatch.setenv("FAKE_OP_DIR", str(root))
    monkeypatch.delenv("FAKE_OP_MULTI", raising=False)
    return FakeOp(root)


@pytest.fixture
def machine(monkeypatch):
    """Pretend to be a given platform/host: machine("macos", "mbp")."""

    def set_machine(platform: str = "wsl", host: str = "pc") -> None:
        monkeypatch.setattr(openv, "platform_name", lambda: platform)
        monkeypatch.setattr(openv, "host_name", lambda: host)

    set_machine()
    return set_machine


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir to work in; project(name) makes another (a second machine's clone)."""

    def make(name: str = "proj", config: str | None = None, files: dict[str, str] | None = None) -> Path:
        path = tmp_path / "machines" / name / "proj"
        path.mkdir(parents=True)
        if config is not None:
            (path / openv.CONFIG_NAME).write_text(config)
        for filename, text in (files or {}).items():
            (path / filename).write_text(text)
        monkeypatch.chdir(path)
        return path

    return make


@pytest.fixture
def run(capsys):
    """Run the CLI in-process; returns (exit code, stdout, stderr)."""

    def run_cli(*argv: str) -> tuple[int, str, str]:
        try:
            code = openv.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        out, err = capsys.readouterr()
        return code, out, err

    return run_cli
