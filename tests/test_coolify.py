import io
import sys

import pytest
from fake_coolify import env

ITEM = "proj .env"
WEB, WORKER = "webuuid0000000000000000", "workeruuid00000000000000"


def config(url: str, extra: str = "") -> str:
    return f"""
[onepassword]
item = "{ITEM}"

[variables]
DEBUG = {{}}

[coolify]
url = "{url}"

[coolify.web]
uuid = "{WEB}"

[coolify.worker]
uuid = "{WORKER}"
{extra}
"""


@pytest.fixture
def apps(coolify):
    coolify.apps[WEB] = [
        env("SECRET_KEY", "s3cret"),
        env("DATABASE_URL", "postgres://prod"),
        env("SENTRY_AUTH_TOKEN", "tok", is_runtime=False),
        env("CERT", "line1\nline2", is_multiline=True, is_literal=True),
        env("ONCE", "hidden", is_shown_once=True),
        env("SECRET_KEY", "preview-only", is_preview=True),
    ]
    coolify.apps[WORKER] = [env("SECRET_KEY", "s3cret"), env("REDIS_URL", "redis://prod")]
    return coolify


@pytest.fixture
def ready(apps, fake_op, project, run, terminal, monkeypatch):
    """A project whose item already holds a working Coolify token; stdin is not a terminal afterwards."""
    project(config=config(apps.url), files={".env": "DEBUG=on\n"})
    terminal(tokens=["good-token"])
    code, _, err = run("coolify", "token", "--yes")
    assert code == 0, err
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    return apps


def section(fake_op, name: str) -> dict[str, str]:
    return {label: v for (sec, label), v in fake_op.values(ITEM).items() if sec == name}


# --- opt-in ---


def test_projects_without_coolify_never_touch_it(fake_op, project, run):
    project(config="[variables]\nDEBUG = {}\n", files={".env": "DEBUG=on\n"})
    code, _, err = run("coolify")
    assert code == 2
    assert "Coolify sync isn't set up for this project" in err
    assert "[coolify]" in err


def test_local_commands_ignore_the_coolify_table(apps, fake_op, project, run):
    project(config=config(apps.url), files={".env": "DEBUG=on\n"})
    assert run("push", "--yes")[0] == 0
    assert run("status")[0] == 0
    assert apps.requests == []  # no token prompt, no API calls


def test_coolify_flags_are_rejected_elsewhere(fake_op, project, run):
    project(config="[variables]\nDEBUG = {}\n")
    code, _, err = run("push", "--prune")
    assert code == 2
    assert "only go with `coolify`" in err


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('[coolify]\n[coolify.web]\nuuid = "x"\n', "needs `url`"),
        ('[coolify]\nurl = "http://c"\n', "lists no apps"),
        ('[coolify]\nurl = "http://c"\n[coolify.web]\nsection = "x"\n', "needs the app's `uuid`"),
        ('[coolify]\nurl = "http://c"\ntoken = "abc"\n', "'token' is neither"),
        ('[coolify]\nurl = "file:///etc"\n[coolify.web]\nuuid = "x"\n', "an http(s) address"),
        ('[coolify]\nurl = "http://c"\n[coolify.web]\nuuid = "x"\nsection = "shared"\n', "is reserved"),
        ('[coolify]\nurl = "http://c"\n[coolify.web]\nuuid = "x"\nsection = "host-pc"\n', "is reserved"),
        ('[coolify]\nurl = "http://c"\n[coolify.web]\nuuid = "x"\nsection = "coolify"\n', "is reserved"),
    ],
)
def test_config_errors(project, run, table, message):
    project(config=table)
    code, _, err = run("coolify")
    assert code == 2
    assert message in err


# --- the API token ---


def test_token_is_prompted_for_and_stored_in_the_item(apps, fake_op, project, run, terminal):
    project(config=config(apps.url))
    asked = terminal(answers="y\n", tokens=["good-token"])
    code, out, _ = run("coolify")
    assert code == 0
    assert len(asked) == 1
    assert "No Coolify API token in 1Password yet." in out
    assert section(fake_op, "coolify") == {"api-token": "good-token", "url": apps.url}
    assert "good-token" not in out

    terminal(tokens=[])  # a second run reads it from 1Password: no prompt
    assert run("coolify")[0] == 0


def test_missing_token_without_a_terminal_explains(apps, fake_op, project, run):
    project(config=config(apps.url))
    code, _, err = run("coolify")
    assert code == 2
    assert "Run `op-env-sync coolify token` in a terminal" in err
    assert "read:sensitive" in err


def test_rejected_token_is_replaced(ready, fake_op, run, terminal):
    del ready.tokens["good-token"]
    ready.tokens["rotated"] = {"read", "write", "read:sensitive", "deploy"}
    asked = terminal(tokens=["rotated"])
    code, out, _ = run("coolify")
    assert code == 0
    assert "Coolify rejected the API token." in out
    assert len(asked) == 1
    assert section(fake_op, "coolify") == {"api-token": "rotated", "url": ready.url}


def test_token_that_cannot_read_values_is_refused(apps, fake_op, project, run, terminal):
    apps.tokens["no-sensitive"] = {"read", "write"}
    project(config=config(apps.url))
    terminal(answers="y\n", tokens=["no-sensitive", "good-token"])
    code, out, err = run("coolify")
    assert code == 0, (out, err)
    assert "lacks the read:sensitive ability" in out
    assert section(fake_op, "coolify") == {"api-token": "good-token", "url": apps.url}


def test_three_bad_tokens_give_up(apps, fake_op, project, run, terminal):
    project(config=config(apps.url))
    terminal(tokens=["a", "b", "c"])
    code, _, err = run("coolify")
    assert code == 2
    assert "Giving up" in err


# --- pull: Coolify -> 1Password ---


def test_pull_mirrors_each_app_into_its_own_section(ready, fake_op, run):
    code, out, _ = run("coolify", "pull")
    assert code == 0
    assert section(fake_op, "coolify-web") == {
        "SECRET_KEY": "s3cret",  # the production value, not the preview one
        "DATABASE_URL": "postgres://prod",
        "SENTRY_AUTH_TOKEN": "tok",
        "CERT": "line1\nline2",
    }
    assert section(fake_op, "coolify-worker") == {"SECRET_KEY": "s3cret", "REDIS_URL": "redis://prod"}
    assert "s3cret" not in out
    assert ("GET", "/api/v1/deploy") not in ready.requests


def test_status_compares_per_app(ready, fake_op, run):
    run("coolify", "pull")
    ready.get(WEB, "DATABASE_URL")["value"] = "postgres://changed"
    ready.apps[WORKER].append(env("NEW_ONE", "x"))
    code, out, _ = run("coolify")
    assert code == 0
    assert "web (webuuid0000000000000000)  <->  section 'coolify-web'" in out
    assert "DATABASE_URL       DIFFERS" in out
    assert "ONCE               shown once in Coolify; value not readable, not synced" in out
    assert "NEW_ONE     only in Coolify -> pull" in out
    assert "postgres://changed" not in out


def test_pull_overwrites_only_with_confirmation(ready, fake_op, run):
    run("coolify", "pull")
    ready.get(WEB, "SECRET_KEY")["value"] = "rotated"
    code, out, _ = run("coolify", "pull")
    assert code == 1
    assert "~ coolify-web/SECRET_KEY" in out
    assert section(fake_op, "coolify-web")["SECRET_KEY"] == "s3cret"
    assert run("coolify", "pull", "--yes")[0] == 0
    assert section(fake_op, "coolify-web")["SECRET_KEY"] == "rotated"


def test_pull_prune_removes_keys_deleted_in_coolify(ready, fake_op, run):
    run("coolify", "pull")
    ready.apps[WORKER] = [e for e in ready.apps[WORKER] if e["key"] != "REDIS_URL"]
    assert run("coolify", "pull", "--yes")[0] == 0
    assert "REDIS_URL" in section(fake_op, "coolify-worker")  # kept without --prune
    code, out, _ = run("coolify", "pull", "--prune", "--yes")
    assert code == 0
    assert "- coolify-worker/REDIS_URL" in out
    assert section(fake_op, "coolify-worker") == {"SECRET_KEY": "s3cret"}


def test_pull_prune_never_empties_a_section_from_an_empty_app(ready, fake_op, run):
    run("coolify", "pull")
    ready.apps[WORKER] = []
    code, out, _ = run("coolify", "pull", "--prune", "--yes")
    assert "worker has no variables in Coolify; not pruning" in out
    assert section(fake_op, "coolify-worker") == {"SECRET_KEY": "s3cret", "REDIS_URL": "redis://prod"}


def test_app_filter(ready, fake_op, run):
    assert run("coolify", "pull", "--app", "worker")[0] == 0
    assert section(fake_op, "coolify-web") == {}
    assert section(fake_op, "coolify-worker")
    code, _, err = run("coolify", "--app", "nope")
    assert code == 2
    assert "Apps: web, worker" in err


def test_custom_section_name(apps, fake_op, project, run, terminal):
    cfg = config(apps.url).replace(f'uuid = "{WORKER}"', f'uuid = "{WORKER}"\nsection = "prod-worker"')
    project(config=cfg)
    terminal(answers="y\n", tokens=["good-token"])
    assert run("coolify", "pull", "--yes")[0] == 0
    assert section(fake_op, "prod-worker") == {"SECRET_KEY": "s3cret", "REDIS_URL": "redis://prod"}


# --- push: 1Password -> Coolify ---


def test_push_updates_values_and_keeps_coolify_flags(ready, fake_op, run, monkeypatch):
    run("coolify", "pull")
    import op_env_sync

    cfg = op_env_sync.load_config(op_env_sync.find_config(op_env_sync.Path.cwd()))
    item = op_env_sync.get_item(cfg)
    op_env_sync.apply_to_item(
        cfg,
        item,
        [("coolify-web", "CERT", "new1\nnew2"), ("coolify-web", "SENTRY_AUTH_TOKEN", "tok2")],
    )
    code, out, _ = run("coolify", "push")
    assert code == 1, "changing existing values needs --yes"
    assert "~ web/CERT" in out
    assert run("coolify", "push", "--yes")[0] == 0

    cert = ready.get(WEB, "CERT")
    assert cert["value"] == "new1\nnew2"
    assert cert["is_multiline"] is True and cert["is_literal"] is True
    sentry = ready.get(WEB, "SENTRY_AUTH_TOKEN")
    assert sentry["value"] == "tok2"
    assert sentry["is_runtime"] is False and sentry["is_buildtime"] is True
    assert ready.get(WEB, "ONCE")["value"] == "hidden"  # shown-once vars are never touched
    preview = next(e for e in ready.apps[WEB] if e["is_preview"])
    assert preview["value"] == "preview-only"


def test_push_asks_before_creating_keys(ready, fake_op, run):
    run("coolify", "pull")
    import op_env_sync

    cfg = op_env_sync.load_config(op_env_sync.find_config(op_env_sync.Path.cwd()))
    op_env_sync.apply_to_item(cfg, op_env_sync.get_item(cfg), [("coolify-worker", "EMAIL_URL", "smtp://x")])
    code, out, err = run("coolify", "push")
    assert code == 1
    assert "+ worker/EMAIL_URL" in out
    assert f"Apply 1 change(s) to Coolify at {ready.url}?" in err
    assert "EMAIL_URL" not in ready.values(WORKER)
    assert run("coolify", "push", "--yes")[0] == 0
    assert ready.values(WORKER)["EMAIL_URL"] == "smtp://x"
    assert ready.get(WORKER, "EMAIL_URL")["is_runtime"] is True


def test_push_prune_deletes_keys_missing_from_1password(ready, fake_op, run):
    run("coolify", "pull", "--app", "worker")
    ready.apps[WORKER].append(env("STRAY", "x"))
    assert run("coolify", "push", "--app", "worker", "--yes")[0] == 0
    assert "STRAY" in ready.values(WORKER)
    code, out, _ = run("coolify", "push", "--app", "worker", "--prune", "--yes")
    assert code == 0
    assert "- worker/STRAY" in out
    assert "STRAY" not in ready.values(WORKER)


def test_push_never_pushes_from_an_empty_section(ready, fake_op, run):
    code, out, _ = run("coolify", "push", "--prune", "--yes")
    assert code == 0
    assert "section 'coolify-web' is empty; run `coolify pull` first" in out
    assert ready.values(WORKER) == {"SECRET_KEY": "s3cret", "REDIS_URL": "redis://prod"}


def test_push_redeploys_only_when_asked(ready, fake_op, run):
    run("coolify", "pull")
    import op_env_sync

    cfg = op_env_sync.load_config(op_env_sync.find_config(op_env_sync.Path.cwd()))
    op_env_sync.apply_to_item(cfg, op_env_sync.get_item(cfg), [("coolify-worker", "NEW", "1")])
    code, out, _ = run("coolify", "push", "--yes")
    assert code == 0
    assert "on the next deploy of worker (or use --redeploy)" in out
    assert ready.deploys == []

    op_env_sync.apply_to_item(cfg, op_env_sync.get_item(cfg), [("coolify-worker", "NEW2", "2")])
    code, out, _ = run("coolify", "push", "--redeploy", "--yes")
    assert code == 0
    assert ready.deploys == [WORKER]
    assert "Redeploying worker." in out


def test_push_without_write_ability_explains(ready, fake_op, run):
    run("coolify", "pull")
    ready.tokens["good-token"] = {"read", "read:sensitive"}
    import op_env_sync

    cfg = op_env_sync.load_config(op_env_sync.find_config(op_env_sync.Path.cwd()))
    op_env_sync.apply_to_item(cfg, op_env_sync.get_item(cfg), [("coolify-worker", "NEW", "1")])
    code, _, err = run("coolify", "push", "--yes")
    assert code == 2
    assert "Coolify refused POST" in err
    assert "write" in err


def test_unreachable_server(fake_op, project, run, terminal):
    project(config=config("http://127.0.0.1:9"))
    terminal(tokens=["good-token"])
    code, _, err = run("coolify")
    assert code == 2
    assert "Can't reach Coolify at http://127.0.0.1:9" in err


def test_created_keys_get_a_preview_copy_that_prune_removes_too(ready, fake_op, run):
    run("coolify", "pull", "--app", "worker")
    import op_env_sync

    cfg = op_env_sync.load_config(op_env_sync.find_config(op_env_sync.Path.cwd()))
    op_env_sync.apply_to_item(cfg, op_env_sync.get_item(cfg), [("coolify-worker", "TEMP", "1")])
    assert run("coolify", "push", "--app", "worker", "--yes")[0] == 0
    assert [e["is_preview"] for e in ready.apps[WORKER] if e["key"] == "TEMP"] == [False, True]

    op_env_sync.apply_to_item(cfg, op_env_sync.get_item(cfg), [], [("coolify-worker", "TEMP")])
    code, out, _ = run("coolify", "push", "--app", "worker", "--prune", "--yes")
    assert code == 0
    assert "- worker/TEMP (and its preview copy)" in out
    assert not [e for e in ready.apps[WORKER] if e["key"] == "TEMP"]


def test_preview_only_keys_are_never_touched(ready, fake_op, run):
    ready.apps[WORKER].append(env("PREVIEW_ONLY", "x", is_preview=True))
    run("coolify", "pull", "--app", "worker")
    assert "PREVIEW_ONLY" not in section(fake_op, "coolify-worker")
    assert run("coolify", "push", "--app", "worker", "--prune", "--yes")[0] == 0
    assert any(e["key"] == "PREVIEW_ONLY" for e in ready.apps[WORKER])


# --- where the token may go ---


def test_stored_token_is_never_sent_to_another_url(ready, fake_op, project, run, coolify):
    import op_env_sync

    path = op_env_sync.find_config(op_env_sync.Path.cwd())
    path.write_text(path.read_text().replace(ready.url, "http://127.0.0.1:9"))
    sent = len(ready.requests)
    code, _, err = run("coolify", "push", "--yes")
    assert code == 2
    assert f"is for {ready.url}" in err
    assert "op-env-sync coolify token" in err
    assert len(ready.requests) == sent


def test_token_command_rebinds_the_token_to_a_new_url(ready, fake_op, run, terminal):
    import op_env_sync

    path = op_env_sync.find_config(op_env_sync.Path.cwd())
    path.write_text(path.read_text().replace(ready.url, ready.url.replace("127.0.0.1", "localhost")))
    terminal(tokens=["good-token"])
    assert run("coolify", "token", "--yes")[0] == 0
    assert section(fake_op, "coolify")["url"] == ready.url.replace("127.0.0.1", "localhost")


def test_a_token_stored_without_a_url_is_bound_on_first_use(apps, fake_op, project, run):
    import op_env_sync

    path = project(config=config(apps.url))
    cfg = op_env_sync.load_config(path / ".env.op.toml")
    op_env_sync.apply_to_item(cfg, None, [("coolify", "api-token", "good-token")])
    assert run("coolify")[0] == 0
    assert section(fake_op, "coolify") == {"api-token": "good-token", "url": apps.url}


def test_redirects_are_not_followed(ready, fake_op, run):
    ready.raw = (302, {"Location": "http://127.0.0.1:9/api/v1/x"}, b"")
    code, _, err = run("coolify")
    assert code == 2
    assert "redirected" in err
    assert "http://127.0.0.1:9/api/v1/x" in err


def test_a_non_json_answer_explains(ready, fake_op, run):
    ready.raw = (200, {"Content-Type": "text/html"}, b"<html>Sign in</html>")
    code, _, err = run("coolify")
    assert code == 2
    assert "didn't return JSON" in err


def test_plain_http_to_a_public_host_warns(fake_op, project, run):
    project(config=config("http://coolify.example.com"))
    _, _, err = run("coolify")
    assert "sends the API token and values unencrypted" in err


def test_plain_http_on_a_private_network_is_quiet(apps, fake_op, project, run):
    project(config=config(apps.url))
    _, _, err = run("coolify")
    assert "unencrypted" not in err


# --- the token is checked against the apps being synced ---


def test_app_filter_does_not_need_the_first_app(ready, fake_op, run):
    del ready.apps[WEB]
    code, _, err = run("coolify", "pull", "--app", "worker", "--yes")
    assert code == 0, err


def test_token_without_read_sensitive_is_refused_even_if_the_first_app_is_empty(apps, fake_op, project, run, terminal):
    apps.apps[WEB] = []
    apps.tokens["no-sensitive"] = {"read", "write"}
    project(config=config(apps.url))
    terminal(answers="y\n", tokens=["no-sensitive", "good-token"])
    code, out, err = run("coolify")
    assert code == 0, (out, err)
    assert "lacks the read:sensitive ability" in out
    assert section(fake_op, "coolify")["api-token"] == "good-token"
