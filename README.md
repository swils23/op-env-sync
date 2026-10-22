# op-env-sync

Sync a project's `.env` files across machines through 1Password.

A small tracked file, `.env.op.toml`, lists which variables travel and how. The values stay in your Git-ignored `.env` locally and in one 1Password item remotely. Values that differ per OS or per machine get their own slots in that item, so pushing from your Linux box never overwrites what your Mac uses.

```console
$ op-env-sync
local:     /home/sam/repos/swils  (platform=wsl, host=sffpc)
1Password: my.1password.com (sam@example.com) / default vault / 'swils .env'

  SECRET_KEY                     shared          in sync
  SENTRY_AUTH_TOKEN              shared          not in 1Password -> push
  LOCAL_LLM_TUNNEL_NETWORK_MODE  wsl             in sync  [.env.local-llm]
```

## Requirements

- [`uv`](https://github.com/astral-sh/uv), which provides `uvx` and fetches Python 3.11+ for you
- The [1Password CLI](https://developer.1password.com/docs/cli/get-started/) (`op`), signed in. Turning on the desktop app integration is easiest. From WSL, op-env-sync also finds the Windows `op.exe` that WinGet installs.

op-env-sync has no dependencies beyond the Python standard library.

## Run

```bash
uvx op-env-sync init      # write a starter .env.op.toml from your current .env
uvx op-env-sync           # status: compare local values with 1Password
uvx op-env-sync push      # copy local values up
uvx op-env-sync pull      # copy values down; on a fresh clone this writes .env from scratch
```

Or install it so `op-env-sync` is on your PATH: `uv tool install op-env-sync`.

To give a project a short command for it, add a [just](https://github.com/casey/just) recipe. Then `just env pull` works on a fresh clone:

```just
# Sync .env files through 1Password per .env.op.toml: just env [status|push|pull] [--yes]
[positional-arguments]
@env *args:
    uvx op-env-sync "$@"
```

`push` and `pull` ask before they overwrite an existing value or create the 1Password item. Pass `--yes` to skip the question (required when nothing can answer it, as in scripts and agents). Values are never printed.

## `.env.op.toml`

```toml
[onepassword]
account = "my.1password.com"   # optional: which signed-in account (see below)
vault = "Private"              # optional: op's default vault when omitted
item = "swils .env"            # optional: "<project dir> .env" when omitted

[variables]
SECRET_KEY = {}                                     # shared by every machine
INTERNAL_IPS = { scope = "platform" }               # one value per OS
COMPOSE_PROFILES = { scope = "host" }               # one value per machine
DATABASE_URL = { scope = "local", default = "postgres://localhost/app" }  # never synced
TUNNEL_MODE = { scope = "platform", file = ".env.local-llm", help_text = "bridge on WSL, host on Mac" }
```

Commit it. It holds layout only, never values. op-env-sync looks for it in the current directory and then each parent, so it works from anywhere inside the project.

| Key | Meaning |
|---|---|
| `scope` | `shared` (default), `platform`, `host`, or `local` |
| `file` | The env file this variable lives in, relative to the project. Default `.env`. One item can hold several files. |
| `default` | Written by `pull` when the variable is missing locally and has no value in 1Password |
| `help_text` | Written as a comment above the variable when `pull` adds it |

`default` and `help_text` mean the same as in [epicenv](https://github.com/epicserve/epicenv). The file has its own name so the two tools never fight over `.env.toml`.

### Scopes

All of a project's values live in one Secure Note, with one section per scope:

| Scope | Section | Use it for |
|---|---|---|
| `shared` | `shared` | Values that are the same everywhere (most secrets) |
| `platform` | `macos`, `wsl`, `linux`, `windows` | Things that differ by OS, like Docker networking or paths |
| `host` | `host-<hostname>` | Per-machine settings |
| `local` | — | Values that never leave the machine |

A machine only reads and writes `shared` plus its own platform and host sections. Without a pinned `vault`, `op` looks for the item in every vault you can see; `status`, `push` and `pull` print the vault it was found in. Each section holds concealed fields named after the variables, so you can also read them elsewhere with a secret reference like `op://Private/swils .env/shared/SECRET_KEY`.

## Several 1Password accounts

When more than one account is signed in, as on a work PC with personal and work accounts, choose one in the order op-env-sync checks:

1. `--account` (and `--vault`) on the command line, for one run
2. `account` / `vault` under `[onepassword]` in `.env.op.toml`, for everyone using the project
3. `op`'s own `OP_ACCOUNT` environment variable, or its default account

Use the account's sign-in address (`my.1password.com`, `acme.1password.com`) rather than a shorthand: the address names the same account on every machine and for every person. `op account list` shows it. `op-env-sync status` prints which account and vault it actually resolved to. Because a missing item is also what the wrong account looks like, `push` never creates the item without asking.

## How syncing works

There is no merge base and no background sync. `status` shows what differs, and you choose a direction:

- **push** copies every tracked local value that differs into its section of the item. Keys that are in `.env` but not in `.env.op.toml` are reported and skipped.
- **pull** copies every value from this machine's sections into the right env file. It changes the value in place and keeps comments, ordering, `export` prefixes and quote style. New files are created with owner-only permissions (`0600`). A symlinked env file is written through, as long as it points inside the project.

1Password keeps every version of the item, so an overwritten value can be recovered from its item history. Your local `.env` has no such history, which is why `pull` asks first.

## Coolify (optional)

If you deploy with [Coolify](https://coolify.io), the same 1Password item can also hold each app's environment variables, one section per app, kept in sync in both directions. Nothing here applies to a project whose `.env.op.toml` has no `[coolify]` table: the other commands never contact Coolify or ask for a token.

```toml
[coolify]
url = "https://coolify.example.com"

[coolify.web]                       # 1Password section "coolify-web"
uuid = "b8c0go000s0kgk8wcw0c0k80"   # the id in the app's Coolify URL

[coolify.worker]
uuid = "jsowo44owwc0gs0scssck80k"
section = "prod-worker"             # optional: a different section name (not shared, local, coolify, a platform or host-*)
```

```bash
op-env-sync coolify           # status: compare each app with its section (key names only)
op-env-sync coolify pull      # Coolify -> 1Password
op-env-sync coolify push      # 1Password -> Coolify
op-env-sync coolify token     # store a new API token
```

Values go straight from one API to the other. They are never written to disk or printed.

**The API token.** The first `coolify` command prompts for a token, with hidden input, and stores it in the item's `coolify` section, so each machine only ever reads it from 1Password. Create the token in Coolify under *Keys & Tokens → API tokens* with the `read`, `write` and `read:sensitive` abilities. Coolify only returns values to tokens with `read:sensitive`. Add `deploy` too if you'll use `--redeploy`. The token is stored with the `url` it was entered for and is never sent anywhere else: if `url` changes, run `coolify token` to store one for the new address. If Coolify rejects a stored token, you're prompted for a replacement, and `coolify token` changes it whenever you like. Redirects aren't followed, and a plain-`http` url outside your private network gets a warning.

**What syncs.** Each app's production variables. Variables for preview deployments are left alone. Coolify never returns the value of a variable marked "shown once", so those are listed but never synced.

**push** creates missing variables (Coolify's defaults: available at build time and runtime) and updates changed ones. An update keeps each variable's Coolify settings (literal, multiline, build-time, runtime) as they are. Every push asks first, naming the server. Coolify applies new values on an app's next deploy; pass `--redeploy` to deploy the changed apps right away. `push` skips an app whose 1Password section is empty, so an app is never emptied by accident; run `coolify pull` first.

**Deleting.** Neither direction deletes anything by default. `--prune` also removes keys the source side doesn't have: `pull --prune` deletes fields from 1Password, and `push --prune` deletes variables in Coolify. Coolify gives every new variable a preview-deployment copy (the same happens when you add one in its UI), so `push --prune` deletes a key's preview copy along with it. `--app NAME` limits any command to one app.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
./op_env_sync.py --help      # the single file also runs directly with uv
```

The tests run against `tests/fake_op.py`, a stand-in for `op` that stores items as JSON per account and vault.

### Releasing

Bump `__version__` in `op_env_sync.py`, commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow publishes to PyPI through trusted publishing and attaches the wheel to a GitHub release. You can rehearse a release on TestPyPI from the Actions tab.
