# openv

Sync a project's `.env` files across machines through 1Password.

A small tracked file, `.env.op.toml`, lists which variables travel and how. The values stay in your Git-ignored `.env` locally and in one 1Password item remotely. Values that differ per OS or per machine get their own slots in that item, so pushing from your Linux box never overwrites what your Mac uses.

```console
$ openv
local:     /home/sam/repos/swils  (platform=wsl, host=sffpc)
1Password: my.1password.com (sam@example.com) / default vault / 'swils .env'

  SECRET_KEY                     shared          in sync
  SENTRY_AUTH_TOKEN              shared          not in 1Password -> push
  LOCAL_LLM_TUNNEL_NETWORK_MODE  wsl             in sync  [.env.local-llm]
```

## Requirements

- [`uv`](https://github.com/astral-sh/uv), which provides `uvx` and fetches Python 3.11+ for you
- The [1Password CLI](https://developer.1password.com/docs/cli/get-started/) (`op`), signed in. Turning on the desktop app integration is easiest. From WSL, openv also finds the Windows `op.exe` that WinGet installs.

openv has no dependencies beyond the Python standard library.

## Run

```bash
uvx openv init      # write a starter .env.op.toml from your current .env
uvx openv           # status: compare local values with 1Password
uvx openv push      # copy local values up
uvx openv pull      # copy values down; on a fresh clone this writes .env from scratch
```

Or install it so `openv` is on your PATH: `uv tool install openv`.

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

Commit it. It holds layout only, never values. openv looks for it in the current directory and then each parent, so it works from anywhere inside the project.

| Key | Meaning |
|---|---|
| `scope` | `shared` (default), `platform`, `host`, or `local` |
| `file` | The env file this variable lives in. Default `.env`. One item can hold several files. |
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

A machine only reads and writes `shared` plus its own platform and host sections. Each section holds concealed fields named after the variables, so you can also read them elsewhere with a secret reference like `op://Private/swils .env/shared/SECRET_KEY`.

## Several 1Password accounts

When more than one account is signed in, as on a work PC with personal and work accounts, choose one in the order openv checks:

1. `--account` (and `--vault`) on the command line, for one run
2. `account` / `vault` under `[onepassword]` in `.env.op.toml`, for everyone using the project
3. `op`'s own `OP_ACCOUNT` environment variable, or its default account

Use the account's sign-in address (`my.1password.com`, `acme.1password.com`) rather than a shorthand: the address names the same account on every machine and for every person. `op account list` shows it. `openv status` prints which account and vault it actually resolved to. Because a missing item is also what the wrong account looks like, `push` never creates the item without asking.

## How syncing works

There is no merge base and no background sync. `status` shows what differs, and you choose a direction:

- **push** copies every tracked local value that differs into its section of the item. Keys that are in `.env` but not in `.env.op.toml` are reported and skipped.
- **pull** copies every value from this machine's sections into the right env file. It changes the value in place and keeps comments, ordering, `export` prefixes and quote style. New files are created with owner-only permissions (`0600`).

1Password keeps every version of the item, so an overwritten value can be recovered from its item history. Your local `.env` has no such history, which is why `pull` asks first.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
./openv.py --help      # the single file also runs directly with uv
```

The tests run against `tests/fake_op.py`, a stand-in for `op` that stores items as JSON per account and vault.

### Releasing

Bump `__version__` in `openv.py`, commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow publishes to PyPI through trusted publishing and attaches the wheel to a GitHub release. You can rehearse a release on TestPyPI from the Actions tab.
