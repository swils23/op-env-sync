import io
import json
import sys
from pathlib import Path

import pytest

import op_env_sync

FAKE_OP = Path(__file__).with_name("fake_op.py")


class FakeOp:
    def __init__(self, root: Path):
        self.root = root

    def item(self, title: str, account: str = "my.1password.com", vault: str = "Private") -> dict | None:
        path = self.root / f"{account}__{vault}__{title}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def values(self, title: str, **where: str) -> dict[tuple[str, str], str]:
        return op_env_sync.remote_values(self.item(title, **where))

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
        monkeypatch.setattr(op_env_sync, "platform_name", lambda: platform)
        monkeypatch.setattr(op_env_sync, "host_name", lambda: host)

    set_machine()
    return set_machine


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir to work in; project(name) makes another (a second machine's clone)."""

    def make(name: str = "proj", config: str | None = None, files: dict[str, str] | None = None) -> Path:
        path = tmp_path / "machines" / name / "proj"
        path.mkdir(parents=True)
        if config is not None:
            (path / op_env_sync.CONFIG_NAME).write_text(config)
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
            code = op_env_sync.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        out, err = capsys.readouterr()
        return code, out, err

    return run_cli


@pytest.fixture
def coolify():
    from fake_coolify import FakeCoolify

    fake = FakeCoolify()
    fake.thread.start()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def terminal(monkeypatch):
    """Pretend stdin is a terminal: terminal(answers="y\\n", tokens=["t1", ...]) feeds input() and getpass()."""

    def use(answers: str = "", tokens: list[str] | None = None) -> list[str]:
        monkeypatch.setattr(sys, "stdin", TTYInput(answers))
        asked: list[str] = []
        queue = list(tokens or [])

        def fake_getpass(prompt: str = "") -> str:
            asked.append(prompt)
            if not queue:
                raise AssertionError(f"unexpected token prompt: {prompt}")
            return queue.pop(0)

        monkeypatch.setattr(op_env_sync.getpass, "getpass", fake_getpass)
        return asked

    return use
