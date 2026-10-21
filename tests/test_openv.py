import stat

import pytest

import openv

CONFIG = """
[onepassword]
item = "proj .env"

[variables]
DEBUG = {}
SECRET_KEY = {}
ALLOWED_HOSTS = {}
LOCAL_ONLY = { scope = "local", default = "dflt" }
TUNNEL_TOKEN = { file = ".env.local-llm" }
MODE = { scope = "platform", file = ".env.local-llm", help_text = "bridge on WSL, host on Mac" }
"""

ENV = '# top\nDEBUG=on\nSECRET_KEY=abc$def # inline\nALLOWED_HOSTS="a,b"\nUNTRACKED=1\n'
LLM = "# tunnel\nTUNNEL_TOKEN=tok\nMODE=bridge\n"


@pytest.fixture
def pushed(fake_op, machine, project, run):
    """A WSL machine that has pushed its .env and .env.local-llm."""
    path = project("wsl", CONFIG, {".env": ENV, ".env.local-llm": LLM})
    code, _, err = run("push", "--yes")
    assert code == 0, err
    return path


# --- push ---


def test_push_asks_before_creating_the_item(fake_op, machine, project, run):
    project(config=CONFIG, files={".env": ENV, ".env.local-llm": LLM})
    code, out, err = run("push")
    assert code == 1
    assert "this creates it in my.1password.com" in out
    assert "--yes" in err
    assert fake_op.item("proj .env") is None


def test_push_writes_synced_values_into_scope_sections(pushed, fake_op):
    assert fake_op.values("proj .env") == {
        ("shared", "DEBUG"): "on",
        ("shared", "SECRET_KEY"): "abc$def",
        ("shared", "ALLOWED_HOSTS"): "a,b",
        ("shared", "TUNNEL_TOKEN"): "tok",
        ("wsl", "MODE"): "bridge",
    }
    fields = fake_op.item("proj .env")["fields"]
    assert all(f["type"] == "CONCEALED" for f in fields if f.get("section"))


def test_push_skips_untracked_and_local_keys(pushed, fake_op, run):
    code, out, _ = run("status")
    assert "but not in .env.op.toml (not synced): UNTRACKED\n" in out
    names = {name for _, name in fake_op.values("proj .env")}
    assert "UNTRACKED" not in names
    assert "LOCAL_ONLY" not in names


def test_push_is_a_no_op_when_in_sync(pushed, fake_op, run):
    calls = len(fake_op.calls())
    code, out, _ = run("push")
    assert code == 0
    assert "up to date" in out
    assert not any(c[:2] == ["item", "edit"] for c in fake_op.calls()[calls:])


def test_push_overwrite_needs_confirmation(pushed, fake_op, run):
    (pushed / ".env").write_text(ENV.replace("DEBUG=on", "DEBUG=off"))
    code, out, _ = run("push")
    assert code == 1
    assert "~ shared/DEBUG" in out
    assert fake_op.values("proj .env")[("shared", "DEBUG")] == "on"

    assert run("push", "--yes")[0] == 0
    assert fake_op.values("proj .env")[("shared", "DEBUG")] == "off"


def test_platform_values_stay_per_machine(pushed, fake_op, machine, project, run):
    machine("macos", "mbp")
    project("mac", CONFIG, {".env": ENV, ".env.local-llm": LLM.replace("bridge", "host")})
    code, out, _ = run("push")
    assert code == 0
    assert "+ macos/MODE" in out
    values = fake_op.values("proj .env")
    assert values[("wsl", "MODE")] == "bridge"
    assert values[("macos", "MODE")] == "host"


def test_host_scope_uses_the_hostname(fake_op, machine, project, run):
    machine("linux", "box1")
    project(config='[variables]\nPROFILE = { scope = "host" }\n', files={".env": "PROFILE=a\n"})
    assert run("push", "--yes")[0] == 0
    assert fake_op.values("proj .env") == {("host-box1", "PROFILE"): "a"}


# --- pull ---


def test_fresh_clone_pull_writes_private_files(pushed, project, run):
    path = project("fresh", CONFIG)
    code, out, _ = run("pull")
    assert code == 0  # only additions, so no confirmation needed
    assert (path / ".env").read_text() == "DEBUG=on\nSECRET_KEY='abc$def'\nALLOWED_HOSTS=a,b\nLOCAL_ONLY=dflt\n"
    assert (path / ".env.local-llm").read_text() == "TUNNEL_TOKEN=tok\n\n# bridge on WSL, host on Mac\nMODE=bridge\n"
    for name in (".env", ".env.local-llm"):
        assert stat.S_IMODE((path / name).stat().st_mode) == 0o600
    assert "+ LOCAL_ONLY  (default)" in out


def test_pull_reads_this_platforms_section(pushed, machine, project, run):
    machine("macos", "mbp")
    path = project("mac", CONFIG)
    code, out, _ = run("pull")
    assert code == 0
    assert "MODE" not in (path / ".env.local-llm").read_text()  # nothing pushed for macos yet
    assert "TUNNEL_TOKEN=tok" in (path / ".env.local-llm").read_text()


def test_pull_updates_values_in_place(pushed, project, run, monkeypatch):
    project("other", CONFIG, {".env": ENV.replace("abc$def", "new value"), ".env.local-llm": LLM})
    assert run("push", "--yes")[0] == 0

    monkeypatch.chdir(pushed)
    code, out, _ = run("pull")
    assert code == 1, "overwriting a local value needs --yes when not interactive"
    assert "~ SECRET_KEY" in out
    assert run("pull", "--yes")[0] == 0
    expected = ENV.replace("SECRET_KEY=abc$def", "SECRET_KEY='new value'") + "LOCAL_ONLY=dflt\n"
    assert (pushed / ".env").read_text() == expected


def test_pull_without_item_explains(fake_op, machine, project, run):
    project(config=CONFIG)
    code, _, err = run("pull")
    assert code == 2
    assert "Run `push`" in err


# --- account and vault ---


def test_account_and_vault_from_config(fake_op, machine, project, run):
    config = CONFIG.replace("[onepassword]", '[onepassword]\naccount = "work.1password.com"\nvault = "Dev"')
    project(config=config, files={".env": ENV, ".env.local-llm": LLM})
    assert run("push", "--yes")[0] == 0
    assert fake_op.item("proj .env") is None
    assert fake_op.item("proj .env", account="work.1password.com", vault="Dev") is not None
    assert all("--account" in c for c in fake_op.calls())

    code, out, _ = run("status")
    assert "1Password: work.1password.com (me@work.1password.com) / Dev / 'proj .env'" in out


def test_flags_override_config(fake_op, machine, project, run):
    config = CONFIG.replace("[onepassword]", '[onepassword]\naccount = "work.1password.com"')
    project(config=config, files={".env": ENV, ".env.local-llm": LLM})
    assert run("push", "--yes", "--account", "my.1password.com", "--vault", "Personal")[0] == 0
    assert fake_op.item("proj .env", account="my.1password.com", vault="Personal") is not None
    assert fake_op.item("proj .env", account="work.1password.com") is None


def test_multiple_accounts_without_one_chosen_explains(fake_op, machine, project, run, monkeypatch):
    monkeypatch.setenv("FAKE_OP_MULTI", "1")
    project(config=CONFIG, files={".env": ENV})
    code, _, err = run("status")
    assert code == 2
    assert "set `account` under [onepassword] in .env.op.toml or pass --account" in err

    assert run("status", "--account", "my.1password.com")[0] == 0


def test_vault_flag_is_not_passed_to_whoami(fake_op, machine, project, run):
    project(config=CONFIG, files={".env": ENV})
    run("status", "--vault", "Dev")
    whoami = [c for c in fake_op.calls() if c[0] == "whoami"]
    assert whoami
    assert all("--vault" not in c for c in whoami)


# --- init and config discovery ---


def test_init_lists_every_key(fake_op, project, run):
    path = project(files={".env": ENV})
    code, _, _ = run("init", "--account", "work.1password.com")
    assert code == 0
    text = (path / ".env.op.toml").read_text()
    assert 'account = "work.1password.com"' in text
    assert '# vault = "Private"' in text
    assert 'item = "proj .env"' in text
    for key in ("DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "UNTRACKED"):
        assert f"{key} = {{}}" in text
    assert run("init")[0] == 2  # never overwrites


def test_config_is_found_from_a_subdirectory(pushed, run, monkeypatch):
    (pushed / "sub" / "deeper").mkdir(parents=True)
    monkeypatch.chdir(pushed / "sub" / "deeper")
    code, out, _ = run("status")
    assert code == 0
    assert "in sync" in out


def test_missing_config(project, run):
    project()
    code, _, err = run("status")
    assert code == 2
    assert "No .env.op.toml" in err


def test_bad_scope(project, run):
    project(config='[variables]\nX = { scope = "global" }\n')
    code, _, err = run("status")
    assert code == 2
    assert "expected one of shared, platform, host, local" in err


# --- .env parsing and writing ---


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ("A=plain", "plain"),
        ("export A=plain", "plain"),
        ("A=", ""),
        ("A=two words # comment", "two words"),
        ("A=x#not-a-comment", "x#not-a-comment"),
        ("A='single $quoted' # c", "single $quoted"),
        ('A="double \\"q\\" \\n" # c', 'double "q" \n'),
        ('A = "spaced"', "spaced"),
    ],
)
def test_decode(tmp_path, line, value):
    path = tmp_path / ".env"
    path.write_text(line + "\n")
    assert openv.EnvFile.read(path).values == {"A": value}


@pytest.mark.parametrize("value", ["plain", "", "a b", "it's", 'say "hi"', "multi\nline", "back\\slash", "$HOME"])
def test_set_round_trips(tmp_path, value):
    path = tmp_path / ".env"
    env = openv.EnvFile.read(path)
    env.set("A", value)
    env.write(path)
    assert openv.EnvFile.read(path).values["A"] == value


def test_set_keeps_quote_style_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# head\nexport A="x" # keep\nB=1\nA=dup\n')
    env = openv.EnvFile.read(path)
    assert env.values["A"] == "dup"  # last assignment wins, as in dotenv
    env.set("A", "y")
    env.set("B", "2")
    env.write(path)
    assert path.read_text() == '# head\nexport A="x" # keep\nB=2\nA=y\n'


def test_write_keeps_existing_permissions(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    path.chmod(0o640)
    env = openv.EnvFile.read(path)
    env.set("A", "2")
    env.write(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
