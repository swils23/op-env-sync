#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""op-env-sync: sync a project's .env files with 1Password, so secrets travel between machines.

A tracked `.env.op.toml` next to the `.env` says which variables sync and how. It
holds layout, never values, so it is safe to commit:

    [onepassword]
    account = "my.1password.com"   # optional; which signed-in account (sign-in address or ID)
    vault = "Private"              # optional; op's default vault when omitted
    item = "swils .env"            # optional; "<project dir> .env" when omitted

    [variables]
    SECRET_KEY = {}                                  # shared by every machine
    INTERNAL_IPS = { scope = "platform" }            # one value per platform
    COMPOSE_PROFILES = { scope = "host" }            # one value per hostname
    DATABASE_URL = { scope = "local", default = "postgres://..." }  # never synced
    TUNNEL_MODE = { scope = "platform", file = ".env.local-llm" }   # another env file

Scopes map to sections of one Secure Note item: `shared` -> "shared",
`platform` -> "macos" / "wsl" / "linux" / "windows", `host` -> "host-<name>".
A machine only ever writes and reads its own platform/host sections, so a WSL
push can't clobber the Mac's value. `help_text` and `default` are honoured too
(the same keys epicenv uses), as comments and as the fallback written by `pull`.

With several 1Password accounts signed in (a work PC with personal and work
accounts), pin the account in the file, or per run with `--account`. A sign-in
address names the same account on every machine. Without either, op's own
OP_ACCOUNT variable or its default account applies. `--vault` overrides the vault.

There is no merge base, so sync is explicit: `status` compares, `push` copies
.env -> 1Password, `pull` copies 1Password -> .env. Overwriting an existing value,
or creating the item, asks first. Values are never printed.

Optionally, the same item can mirror a Coolify server's application env vars,
one section per app, straight between the two APIs (never via local files):

    [coolify]
    url = "http://10.0.1.50:8000"
    [coolify.swils-web]                  # section "coolify-swils-web"
    uuid = "b8c0go000s0kgk8wcw0c0k80"

`coolify pull` copies Coolify -> 1Password, `coolify push` copies 1Password ->
Coolify. The API token is prompted for once and kept in the item's "coolify"
section. Projects without a [coolify] table never touch Coolify.

Usage: op-env-sync [status|push|pull|init] [--yes] [--account ACCOUNT] [--vault VAULT]
       op-env-sync coolify [status|pull|push|token] [--app NAME] [--prune] [--redeploy] [--yes]
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.2.0"

CONFIG_NAME = ".env.op.toml"
ENV_NAME = ".env"
SCOPES = ("shared", "platform", "host", "local")
NOTE = "Managed by op-env-sync from {project}/.env.op.toml. Sections: shared, one per platform, one per host."

# Where the Coolify API token lives in the item. (Names, not secrets: hence the noqa.)
TOKEN_SECTION = "coolify"  # noqa: S105
TOKEN_FIELD = "api-token"  # noqa: S105
ABILITIES_HELP = (
    "Create one in Coolify under Keys & Tokens > API tokens with the read, write and read:sensitive"
    " abilities (plus deploy, for --redeploy)."
)

# 1Password CLI installed on the Windows side, reachable from WSL without being on PATH.
WSL_OP_GLOB = "/mnt/c/Users/*/AppData/Local/Microsoft/WinGet/Packages/AgileBits.1Password.CLI_*/op.exe"


# --- Machine identity ---


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return "wsl"
    except OSError:
        pass
    return "linux"


def host_name() -> str:
    return socket.gethostname().split(".")[0].lower()


def section_for(scope: str) -> str:
    if scope == "platform":
        return platform_name()
    if scope == "host":
        return f"host-{host_name()}"
    return scope  # "shared", or "local" (never sent to 1Password)


# --- .env.op.toml ---


@dataclass
class Var:
    name: str
    scope: str = "shared"
    help_text: str = ""
    default: str | None = None
    file: str = ENV_NAME

    @property
    def section(self) -> str:
        return section_for(self.scope)


@dataclass
class CoolifyApp:
    name: str
    uuid: str
    section: str


@dataclass
class Coolify:
    url: str
    apps: list[CoolifyApp]


@dataclass
class Config:
    project: Path
    account: str | None
    vault: str | None
    item: str
    variables: dict[str, Var] = field(default_factory=dict)
    coolify: Coolify | None = None

    @property
    def files(self) -> list[str]:
        return list(dict.fromkeys([ENV_NAME, *(v.file for v in self.variables.values())]))

    @property
    def synced(self) -> list[Var]:
        return [v for v in self.variables.values() if v.scope != "local"]


class Locals:
    """Every env file the config mentions, read once."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.files = {name: EnvFile.read(cfg.project / name) for name in cfg.files}

    def get(self, var: Var) -> str | None:
        return self.files[var.file].values.get(var.name)

    def untracked(self) -> list[str]:
        tracked = {(v.file, v.name) for v in self.cfg.variables.values()}
        return [
            name if file == ENV_NAME else f"{file}:{name}"
            for file, env in self.files.items()
            for name in env.values
            if (file, name) not in tracked
        ]


def find_config(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    return None


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    op = data.get("onepassword", {})
    cfg = Config(
        project=path.parent,
        account=op.get("account"),
        vault=op.get("vault"),
        item=op.get("item") or f"{path.parent.name} .env",
    )
    for name, spec in data.get("variables", {}).items():
        scope = spec.get("scope", "shared")
        if scope not in SCOPES:
            die(f"{path}: {name} has scope {scope!r}; expected one of {', '.join(SCOPES)}")
        default = spec.get("default")
        cfg.variables[name] = Var(
            name,
            scope,
            spec.get("help_text", ""),
            None if default is None else _toml_scalar(default),
            spec.get("file", ENV_NAME),
        )
    if "coolify" in data:
        cfg.coolify = _load_coolify(path, data["coolify"])
    return cfg


def _load_coolify(path: Path, table: dict) -> Coolify:
    apps = []
    for name, value in table.items():
        if name == "url":
            continue
        if not isinstance(value, dict):
            die(f"{path}: [coolify] takes `url` plus one [coolify.<app>] table per app; {name!r} is neither.")
        if not value.get("uuid"):
            die(f"{path}: [coolify.{name}] needs the app's `uuid` (the id in its Coolify URL).")
        apps.append(CoolifyApp(name, value["uuid"], value.get("section") or f"coolify-{name}"))
    if not table.get("url"):
        die(f"{path}: [coolify] needs `url`, the Coolify server's address.")
    if not apps:
        die(f"{path}: [coolify] lists no apps. Add a [coolify.<app>] table with the app's `uuid`.")
    return Coolify(table["url"].rstrip("/"), apps)


def _toml_scalar(value: object) -> str:
    # TOML booleans come back as Python bools; .env wants the dotenv spelling.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- .env parsing (line-preserving) ---

ASSIGN_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)")
BARE_SAFE_RE = re.compile(r"[A-Za-z0-9_./:@,+=%~-]*")


@dataclass
class EnvFile:
    """A .env kept as its lines, so updates touch only the value and keep comments/order."""

    lines: list[str]
    # name -> (line index, value start, value end) of the last assignment (dotenv: last wins)
    spans: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path) -> EnvFile:
        env = cls(path.read_text().splitlines() if path.exists() else [])
        for i, line in enumerate(env.lines):
            m = ASSIGN_RE.match(line)
            if m:
                value, end = _decode(line, m.end())
                env.spans[m.group(2)] = (i, m.end(), end)
                env.values[m.group(2)] = value
        return env

    def set(self, name: str, value: str, help_text: str = "") -> None:
        if name in self.spans:
            i, start, end = self.spans[name]
            old = self.lines[i][start:end]
            self.lines[i] = self.lines[i][:start] + _encode(value, old[:1]) + self.lines[i][end:]
        else:
            if help_text and self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines.extend(f"# {ln}" for ln in help_text.splitlines())
            self.lines.append(f"{name}={_encode(value, '')}")
        self.values[name] = value

    def write(self, path: Path) -> None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.op-env-sync-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(self.lines) + "\n")
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _decode(line: str, start: int) -> tuple[str, int]:
    """Return (value, end offset) for the value starting at `start`; the rest is comment/whitespace."""
    quote = line[start : start + 1]
    if quote == "'":
        end = line.find("'", start + 1)
        if end != -1:
            return line[start + 1 : end], end + 1
    elif quote == '"':
        out, i = [], start + 1
        while i < len(line):
            c = line[i]
            if c == "\\" and i + 1 < len(line):
                out.append({"n": "\n", "t": "\t"}.get(line[i + 1], line[i + 1]))
                i += 2
            elif c == '"':
                return "".join(out), i + 1
            else:
                out.append(c)
                i += 1
    # Unquoted (or unterminated): runs to an inline " #" comment.
    m = re.search(r"\s+#", line[start:])
    end = start + m.start() if m else len(line.rstrip())
    end = max(end, start)
    return line[start:end].strip(), end


def _encode(value: str, prefer_quote: str) -> str:
    if BARE_SAFE_RE.fullmatch(value) and prefer_quote not in ("'", '"'):
        return value
    if prefer_quote != '"' and "'" not in value and "\n" not in value:
        return f"'{value}'"
    # No `$` escaping: dotenv readers disagree on `\$` (python-dotenv keeps the backslash).
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


# --- 1Password CLI ---


def op_binary() -> str:
    for name in ("op", "op.exe"):
        if found := shutil.which(name):
            return found
    if found := sorted(glob.glob(WSL_OP_GLOB)):
        return found[-1]
    die("1Password CLI not found. Install it: https://developer.1password.com/docs/cli/get-started/")


def run_op(cfg: Config, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [op_binary(), *args]
    if cfg.account:
        cmd += ["--account", cfg.account]  # a global flag; valid on every op command
    if cfg.vault and args[0] == "item":
        cmd += ["--vault", cfg.vault]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8")


def op_failed(what: str, stderr: str) -> None:
    msg = f"{what}:\n{stderr.strip()}"
    if "account" in stderr.lower():
        msg += (
            f"\n\nWith several 1Password accounts signed in, set `account` under [onepassword] in {CONFIG_NAME}"
            " or pass --account. `op account list` shows the sign-in addresses."
        )
    die(msg)


def op_json(cfg: Config, *args: str, stdin: str | None = None) -> dict:
    proc = run_op(cfg, *args, "--format", "json", stdin=stdin)
    if proc.returncode != 0:
        op_failed(f"op {args[0]} {args[1]} failed", proc.stderr)
    return json.loads(proc.stdout)


def get_item(cfg: Config) -> dict | None:
    proc = run_op(cfg, "item", "get", cfg.item, "--format", "json")
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    if "isn't an item" in proc.stderr:
        return None
    op_failed(f"Couldn't read 1Password item {cfg.item!r}", proc.stderr)


def describe_target(cfg: Config) -> str:
    """Where the item lives, naming the account op actually resolved to (best effort)."""
    proc = run_op(cfg, "whoami", "--format", "json")
    account = cfg.account or "default account"
    if proc.returncode == 0:
        me = json.loads(proc.stdout)
        account = f"{me.get('url', account)} ({me.get('email', '?')})"
    return f"{account} / {cfg.vault or 'default vault'} / {cfg.item!r}"


def remote_values(item: dict | None) -> dict[tuple[str, str], str]:
    """(section label, field label) -> value for every custom field in a section."""
    if not item:
        return {}
    return {
        (f["section"]["label"], f["label"]): f.get("value", "")
        for f in item.get("fields", [])
        if f.get("section", {}).get("label")
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def apply_to_item(
    cfg: Config,
    item: dict | None,
    sets: list[tuple[str, str, str]],
    deletes: list[tuple[str, str]] = (),
) -> None:
    """Write (section, label, value) fields and drop (section, label) fields in one item edit or create."""
    if item is None:
        item = {
            "title": cfg.item,
            "category": "SECURE_NOTE",
            "fields": [
                {
                    "id": "notesPlain",
                    "type": "STRING",
                    "purpose": "NOTES",
                    "label": "notesPlain",
                    "value": NOTE.format(project=cfg.project.name),
                }
            ],
            "sections": [],
        }
    sections = item.setdefault("sections", [])
    gone = set(deletes)
    fields = [f for f in item.get("fields", []) if (f.get("section", {}).get("label"), f.get("label")) not in gone]
    item["fields"] = fields
    for section_label, label, value in sets:
        section = next((s for s in sections if s.get("label") == section_label), None)
        if section is None:
            section = {"id": f"op_env_sync_{_slug(section_label)}", "label": section_label}
            sections.append(section)
        existing = next(
            (f for f in fields if f.get("label") == label and f.get("section", {}).get("label") == section_label),
            None,
        )
        if existing:
            existing["value"] = value
        else:
            fields.append(
                {
                    "id": f"{section['id']}_{_slug(label)}",
                    "type": "CONCEALED",
                    "label": label,
                    "value": value,
                    "section": {"id": section["id"], "label": section["label"]},
                }
            )
    used = {f.get("section", {}).get("label") for f in fields}
    item["sections"] = [s for s in sections if s.get("label") in used]
    payload = json.dumps(item)
    if "id" in item:
        op_json(cfg, "item", "edit", item["id"], stdin=payload)
    else:
        op_json(cfg, "item", "create", "-", stdin=payload)


# --- Commands ---


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    local = Locals(cfg)
    item = get_item(cfg)
    remote = remote_values(item)
    print(f"local:     {cfg.project}  (platform={platform_name()}, host={host_name()})")
    print(f"1Password: {describe_target(cfg)}" + ("" if item else "  (item not created yet)") + "\n")

    width = max((len(n) for n in cfg.variables), default=0)
    for var in cfg.variables.values():
        lv = local.get(var)
        if var.scope == "local":
            state = "local only" if lv is not None else ("missing (pull writes default)" if var.default else "missing")
        else:
            rv = remote.get((var.section, var.name))
            if lv is None and rv is None:
                state = "missing everywhere"
            elif rv is None:
                state = "not in 1Password -> push"
            elif lv is None:
                state = f"not in {var.file} -> pull"
            elif lv != rv:
                state = "DIFFERS"
            else:
                state = "in sync"
        file = "" if var.file == ENV_NAME else f"  [{var.file}]"
        print(f"  {var.name:<{width}}  {var.section:<14}  {state}{file}")

    if untracked := local.untracked():
        print(f"\nIn env files but not in {CONFIG_NAME} (not synced): {', '.join(untracked)}")
    mine = {v.section for v in cfg.synced}
    stale = sorted(f"{s}/{n}" for s, n in remote if s in mine and n not in cfg.variables)
    if stale:
        print(f"In 1Password but not in {CONFIG_NAME} (ignored): {', '.join(stale)}")
    return 0


def cmd_push(cfg: Config, args: argparse.Namespace) -> int:
    local = Locals(cfg)
    item = get_item(cfg)
    remote = remote_values(item)
    updates, overwrites = [], []
    for var in cfg.synced:
        lv = local.get(var)
        if lv is None:
            continue
        rv = remote.get((var.section, var.name))
        if rv != lv:
            updates.append(var)
            if rv is not None:
                overwrites.append(var)
    if untracked := local.untracked():
        print(f"Skipping keys not in {CONFIG_NAME}: {', '.join(untracked)}")
    if not updates:
        print("1Password is up to date.")
        return 0
    for var in updates:
        print(f"  {'~' if var in overwrites else '+'} {var.section}/{var.name}")
    if item is None:
        # A missing item is also what the wrong account or vault looks like, so creating one is never silent.
        print(f"No item yet; this creates it in {describe_target(cfg)}.")
        if not confirm("Create it?", args.yes):
            return 1
    elif overwrites and not confirm(f"Overwrite {len(overwrites)} value(s) in 1Password?", args.yes):
        return 1
    apply_to_item(cfg, item, [(v.section, v.name, local.get(v) or "") for v in updates])
    print(f"Pushed {len(updates)} value(s) to {cfg.item!r}.")
    return 0


def cmd_pull(cfg: Config, args: argparse.Namespace) -> int:
    local = Locals(cfg)
    item = get_item(cfg)
    if item is None:
        die(f"No 1Password item {cfg.item!r} yet. Run `push` on a machine that has the .env.")
    remote = remote_values(item)
    changes: list[tuple[Var, str, str]] = []  # (var, value, where it came from)
    for var in cfg.variables.values():
        lv = local.get(var)
        rv = None if var.scope == "local" else remote.get((var.section, var.name))
        source = var.section
        if rv is None and lv is None and var.default is not None:
            rv, source = var.default, "default"
        if rv is not None and lv != rv:
            changes.append((var, rv, source))
    if not changes:
        print("Env files are up to date.")
        return 0
    overwrites = [v for v, _, _ in changes if local.get(v) is not None]
    for var, _, source in changes:
        file = "" if var.file == ENV_NAME else f"  [{var.file}]"
        print(f"  {'~' if var in overwrites else '+'} {var.name}  ({source}){file}")
    if overwrites and not confirm(f"Overwrite {len(overwrites)} local value(s)?", args.yes):
        return 1
    for var, value, _ in changes:
        local.files[var.file].set(var.name, value, var.help_text)
    for name in dict.fromkeys(v.file for v, _, _ in changes):
        local.files[name].write(cfg.project / name)
        print(f"Wrote {cfg.project / name}")
    return 0


def cmd_init(_cfg: None, args: argparse.Namespace) -> int:
    path = Path.cwd() / CONFIG_NAME
    if path.exists():
        die(f"{path} already exists.")
    local = EnvFile.read(Path.cwd() / ENV_NAME)
    lines = [
        "# Which .env variables sync to 1Password, and how. Values never live here.",
        "# Scopes: shared (default), platform (per OS), host (per machine), local (never synced).",
        "# Sync with `op-env-sync status|push|pull` (https://github.com/swils23/op-env-sync).",
        "",
        "[onepassword]",
        f'account = "{args.account}"'
        if args.account
        else '# account = "my.1password.com"  # pin it when several accounts are signed in',
        f'vault = "{args.vault}"' if args.vault else '# vault = "Private"',
        f'item = "{Path.cwd().name} .env"',
        "",
        "[variables]",
        *(f"{name} = {{}}" for name in local.values),
    ]
    path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {path} with {len(local.values)} variable(s), all shared. Mark per-machine ones, then push.")
    return 0


# --- Coolify (optional; only with a [coolify] table) ---


class TokenRejected(Exception):
    """Coolify answered 401, or the token can't see values."""


class CoolifyClient:
    """The few Coolify v4 API calls op-env-sync needs. Values only ever pass through memory."""

    def __init__(self, url: str, token: str):
        self.base = f"{url}/api/v1"
        self.url = url
        self.token = token

    def request(self, method: str, path: str, body: object = None, query: dict | None = None) -> object:
        url = self.base + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        req = urllib.request.Request(  # noqa: S310 - the URL comes from the project's own config
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            with exc:  # an unclosed error response leaks its socket
                detail = exc.read().decode(errors="replace")
            if exc.code == 401:
                raise TokenRejected("Coolify rejected the API token.") from None
            with contextlib.suppress(ValueError, AttributeError):
                detail = json.loads(detail).get("message", detail)
            if exc.code == 403:
                die(f"Coolify refused {method} {path}: {detail}\n{ABILITIES_HELP}")
            die(f"Coolify {method} {path} failed ({exc.code}): {detail}")
        except urllib.error.URLError as exc:
            die(f"Can't reach Coolify at {self.url}: {exc.reason}")
        return json.loads(raw) if raw else None

    def envs(self, app: CoolifyApp) -> list[dict]:
        """The app's production env vars (preview-deployment ones are left alone)."""
        envs = [e for e in self.request("GET", f"/applications/{app.uuid}/envs") if not e.get("is_preview")]
        if any("value" not in e for e in envs if not e.get("is_shown_once")):
            raise TokenRejected("This Coolify token can't read values (it lacks the read:sensitive ability).")
        return envs

    def create(self, app: CoolifyApp, key: str, value: str) -> None:
        self.request(
            "POST",
            f"/applications/{app.uuid}/envs",
            {"key": key, "value": value, "is_preview": False, "is_multiline": "\n" in value},
        )

    def update(self, app: CoolifyApp, env: dict, value: str) -> None:
        # Coolify resets is_literal / is_multiline / is_shown_once when an update leaves them out,
        # so send every flag as it stands; only the value changes.
        body = {"key": env["key"], "value": value, "is_preview": False}
        for flag in ("is_literal", "is_multiline", "is_shown_once", "is_runtime", "is_buildtime"):
            if flag in env:
                body[flag] = bool(env[flag])
        self.request("PATCH", f"/applications/{app.uuid}/envs", body)

    def delete(self, app: CoolifyApp, env: dict) -> None:
        self.request("DELETE", f"/applications/{app.uuid}/envs/{env['uuid']}")

    def deploy(self, apps: list[CoolifyApp]) -> object:
        return self.request("POST", "/deploy", query={"uuid": ",".join(a.uuid for a in apps)})


def prompt_token(cfg: Config, reason: str) -> str:
    if not sys.stdin.isatty():
        die(f"{reason} Run `op-env-sync coolify token` in a terminal to store one. {ABILITIES_HELP}")
    print(f"{reason}\n{ABILITIES_HELP}")
    token = getpass.getpass(f"Coolify API token for {cfg.coolify.url}: ").strip()
    if not token:
        die("No token entered.")
    return token


def coolify_session(cfg: Config, args: argparse.Namespace) -> tuple[CoolifyClient, dict]:
    """A working client plus the (possibly just created) item; prompts for and stores the token as needed."""
    item = get_item(cfg)
    token = remote_values(item).get((TOKEN_SECTION, TOKEN_FIELD))
    reason = "No Coolify API token in 1Password yet."
    if args.action == "token":
        token, reason = None, "Enter a new Coolify API token."
    prompts = 0
    while True:
        if not token:
            if prompts == 3:
                die(f"{reason} Giving up after three tries.")
            token = prompt_token(cfg, reason)
            prompts += 1
        client = CoolifyClient(cfg.coolify.url, token)
        try:
            client.envs(cfg.coolify.apps[0])
        except TokenRejected as exc:
            reason, token = str(exc), None
            continue
        if token != remote_values(item).get((TOKEN_SECTION, TOKEN_FIELD)):
            save_token(cfg, item, token, args)
            item = get_item(cfg)
        return client, item


def save_token(cfg: Config, item: dict | None, token: str, args: argparse.Namespace) -> None:
    if item is None:
        print(f"No item yet; storing the token creates it in {describe_target(cfg)}.")
        if not confirm("Create it?", args.yes):
            raise SystemExit(1)
    apply_to_item(cfg, item, [(TOKEN_SECTION, TOKEN_FIELD, token)])
    print(f"Saved the Coolify token in {cfg.item!r} (section {TOKEN_SECTION!r}).")


@dataclass
class AppState:
    app: CoolifyApp
    live: dict[str, dict]  # key -> Coolify env (values readable)
    stored: dict[str, str]  # key -> value in the app's 1Password section
    hidden: list[str]  # "shown once" keys: Coolify never returns their values


def app_states(cfg: Config, client: CoolifyClient, item: dict, only: str | None) -> list[AppState]:
    apps = [a for a in cfg.coolify.apps if only in (None, a.name)]
    if not apps:
        die(f"No [coolify.{only}] in {CONFIG_NAME}. Apps: {', '.join(a.name for a in cfg.coolify.apps)}")
    remote = remote_values(item)
    states = []
    for app in apps:
        envs = client.envs(app)
        states.append(
            AppState(
                app,
                live={e["key"]: e for e in envs if not e.get("is_shown_once")},
                stored={label: v for (sec, label), v in remote.items() if sec == app.section},
                hidden=sorted(e["key"] for e in envs if e.get("is_shown_once")),
            )
        )
    return states


def _value(env: dict) -> str:
    return env.get("value") or ""


def cmd_coolify(cfg: Config, args: argparse.Namespace) -> int:
    if cfg.coolify is None:
        die(
            f"Coolify sync isn't set up for this project. To use it, add to {CONFIG_NAME}:\n\n"
            '    [coolify]\n    url = "https://coolify.example.com"\n\n'
            '    [coolify.my-app]\n    uuid = "<the app\'s uuid from its Coolify URL>"'
        )
    client, item = coolify_session(cfg, args)
    if args.action == "token":
        return 0
    states = app_states(cfg, client, item, args.app)
    return {"status": coolify_status, "pull": coolify_pull, "push": coolify_push}[args.action](
        cfg, args, client, item, states
    )


def coolify_status(cfg: Config, _args, _client, _item, states: list[AppState]) -> int:
    print(f"Coolify:   {cfg.coolify.url}")
    print(f"1Password: {describe_target(cfg)}")
    for st in states:
        print(f"\n{st.app.name} ({st.app.uuid})  <->  section {st.app.section!r}")
        keys = sorted(set(st.live) | set(st.stored) | set(st.hidden))
        width = max((len(k) for k in keys), default=0)
        for key in keys:
            if key in st.hidden:
                state = "shown once in Coolify; value not readable, not synced"
            elif key not in st.stored:
                state = "only in Coolify -> pull"
            elif key not in st.live:
                state = "only in 1Password -> push"
            elif _value(st.live[key]) != st.stored[key]:
                state = "DIFFERS"
            else:
                state = "in sync"
            print(f"  {key:<{width}}  {state}")
        if not keys:
            print("  (no variables on either side)")
    return 0


def coolify_pull(cfg: Config, args, _client, item: dict, states: list[AppState]) -> int:
    sets, deletes, overwrites = [], [], 0
    for st in states:
        for key, env in st.live.items():
            if st.stored.get(key) != _value(env):
                sets.append((st.app.section, key, _value(env)))
                overwrites += key in st.stored
                print(f"  {'~' if key in st.stored else '+'} {st.app.section}/{key}")
        if args.prune:
            if not st.live:
                print(f"  ! {st.app.name} has no variables in Coolify; not pruning its section")
                continue
            for key in sorted(set(st.stored) - set(st.live) - set(st.hidden)):
                deletes.append((st.app.section, key))
                print(f"  - {st.app.section}/{key}")
    if not sets and not deletes:
        print("1Password matches Coolify.")
        return 0
    if (overwrites or deletes) and not confirm(
        f"Overwrite {overwrites} and delete {len(deletes)} value(s) in 1Password?", args.yes
    ):
        return 1
    apply_to_item(cfg, item, sets, deletes)
    print(f"Pulled {len(sets)} value(s) from Coolify" + (f", removed {len(deletes)}" if deletes else "") + ".")
    return 0


def coolify_push(cfg: Config, args, client: CoolifyClient, _item, states: list[AppState]) -> int:
    plan: list[tuple[AppState, str, str]] = []  # (state, action, key); action is create/update/delete
    for st in states:
        if not st.stored:
            # Never push (or prune) from an empty section: that would wipe the app's variables.
            print(f"  ! {st.app.name}: section {st.app.section!r} is empty; run `coolify pull` first")
            continue
        for key in sorted(st.stored):
            if key in st.hidden:
                print(f"  ! {st.app.name}/{key} is shown-once in Coolify; left alone")
            elif key not in st.live:
                plan.append((st, "create", key))
            elif _value(st.live[key]) != st.stored[key]:
                plan.append((st, "update", key))
        if args.prune:
            plan += [(st, "delete", key) for key in sorted(set(st.live) - set(st.stored))]
    if not plan:
        print("Coolify matches 1Password.")
        return 0
    symbol = {"create": "+", "update": "~", "delete": "-"}
    for st, action, key in plan:
        print(f"  {symbol[action]} {st.app.name}/{key}")
    risky = sum(action != "create" for _, action, _ in plan)
    if risky and not confirm(f"Change {risky} existing value(s) in Coolify?", args.yes):
        return 1
    for st, action, key in plan:
        if action == "create":
            client.create(st.app, key, st.stored[key])
        elif action == "update":
            client.update(st.app, st.live[key], st.stored[key])
        else:
            client.delete(st.app, st.live[key])
    changed = list({id(st): st.app for st, _, _ in plan}.values())
    print(f"Pushed {len(plan)} change(s) to Coolify.")
    if args.redeploy:
        client.deploy(changed)
        print(f"Redeploying {', '.join(a.name for a in changed)}.")
    else:
        print(f"Coolify applies them on the next deploy of {', '.join(a.name for a in changed)} (or use --redeploy).")
    return 0


# --- CLI plumbing ---


def die(msg: str) -> None:
    print(f"op-env-sync: {msg}", file=sys.stderr)
    raise SystemExit(2)


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} Re-run with --yes to confirm non-interactively.", file=sys.stderr)
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="op-env-sync",
        description="Sync a project's .env files with 1Password, so secrets travel between machines.",
    )
    parser.add_argument("command", nargs="?", default="status", choices=("status", "push", "pull", "init", "coolify"))
    parser.add_argument(
        "action",
        nargs="?",
        choices=("status", "pull", "push", "token"),
        help="with `coolify`: status (default), pull (Coolify -> 1Password), push (1Password -> Coolify),"
        " or token (store a new API token)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="overwrite or create without asking")
    parser.add_argument("--app", help="with `coolify`: only this [coolify.<app>]")
    parser.add_argument("--prune", action="store_true", help="with `coolify`: also delete keys the source lacks")
    parser.add_argument("--redeploy", action="store_true", help="with `coolify push`: redeploy the changed apps")
    parser.add_argument("--account", help=f"1Password account (sign-in address or ID); overrides {CONFIG_NAME}")
    parser.add_argument("--vault", help=f"1Password vault; overrides {CONFIG_NAME}")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.command == "coolify":
        args.action = args.action or "status"
    elif args.action or args.app or args.prune or args.redeploy:
        parser.error("a second command, --app, --prune and --redeploy only go with `coolify`")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # keep the change list ahead of a stderr prompt

    if args.command == "init":
        return cmd_init(None, args)
    path = find_config(Path.cwd())
    if path is None:
        die(f"No {CONFIG_NAME} here or in any parent. Run `init` in the project root to create one.")
    cfg = load_config(path)
    cfg.account = args.account or cfg.account
    cfg.vault = args.vault or cfg.vault
    try:
        commands = {"status": cmd_status, "push": cmd_push, "pull": cmd_pull, "coolify": cmd_coolify}
        return commands[args.command](cfg, args)
    except BrokenPipeError:
        # Output piped into something like `head` that stopped reading: exit quietly, as other CLIs do.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


if __name__ == "__main__":
    sys.exit(main())
