# Boring Pirates

A Pirate is a local AI agent that a Pond can give work to. Each Pirate is one
instance bound to one AI provider (Mistral or an OpenAI-compatible server), running under its own Unix
account with its own home, handbook and work folder. It serves one or more
**flavours**: configurations of that provider's models with their own
handbook, tools and budgets. A Pond agent hands a flavour an assignment as
a job (`job_start(pirate=...)`) and reads the result when it is done.

Part of the [Pond family](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md);
shared libraries from [Pond Common](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/README.md).
All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/pirates-dark.svg">
  <img alt="A Pirate, a local agent a Pond gives work to: the Pirate registers its flavours with each Pond it may serve and answers its heartbeat; the Pond's worker hands one flavour an assignment over a local socket. Inside the Pirate's own Unix account and systemd sandbox, the Pirate keeps its state folder with the API key and transcripts, kept even from its own file tools; its home with the handbook and work, the only writable folder; and an executor that runs commands in a systemd unit with a deadline. It asks the Crocodile's service authority for a 60-second token for one service, calls the services its grants allow, and calls its model provider, a cloud API or a server on the LAN." src="docs/pirates.svg">
</picture>

## Why

A Pond's own model is not always the right worker. Some work is cheaper on a
smaller model, some should run in parallel while the conversation carries on,
and some deserves a second, independent pair of eyes. A Pirate is that
worker, with its own account and its own grants — what it can touch is its
own permission, never the calling Pond's. And when it reports back, trust
its recorded writes, not its prose: a model sometimes claims work that never
happened, so the result lists what was actually done.

For a first run, start with [installation](#installing-a-pirate) and
[the result](#a-run-and-its-result). For an existing Pirate, use
[configuration](docs/CONFIGURATION.md), [updates](#updating-pirates) or
[removal](#removing-a-pirate).

## Installing a Pirate

Follow the family task page, [Add a Pirate](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/add-pirate.md),
for download, installation, admission and the first check. This section
lists first-install settings. Linux runtime requirements are x86_64,
glibc 2.39 or newer, systemd and Python 3.12+ for deploy.

| Setting | Meaning |
|---|---|
| `--install --name NAME` | Add that Pirate, including when others are installed; NAME matches `[a-z][a-z0-9]{0,15}` |
| `--ponds a,b\|all` | Currently enabled Ponds admitted to it |
| `--account ACCOUNT` | Dedicated account; default pirate-NAME |
| `--display-name TEXT` | Name shown to clients |
| `--provider`, `--model` | Inference destination/model; default provider mistral |
| `--key-file PATH` | Read the provider key instead of entering it privately |
| `--groups` | Explicit extra Unix groups |
| `--lab yes` | Add an experimental lab flavour within its configured caps |

A `--key-file` input and its parent directories must be root-owned, with no
other writer and no symlinks. Stage the key in a protected root-owned folder
before previewing the install. The preview shows its path, owner and mode;
it does not show the key.

The installer creates the account, home/work/handbook, config, state and
units, and asks the Crocodile for admission with typed APPLY. It keeps the
provider key root-only; [key configuration](docs/CONFIGURATION.md#the-api-key)
owns later changes. The release checks its installed Crocodile requirement
before preparing or switching code. Admission restarts the controller and
every enabled Pond worker; the shared deploy then restarts the running Pirates.
Finish or stop affected assignments first. The Pond lists the configured
Pirate name `NAME`; `--account` selects its Unix identity.

After installation, `config.json` belongs to the operator. First-install
settings are refused for an existing Pirate. Admit a later Pond with
`sudo boring-crocodile-admin pirate-add NAME --ponds a,b --apply`.
The description records the pairing; the running Pirate discovers and verifies
its new workers without a configuration edit or Pirate restart:
[admission reference](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/INSTALL.md#a-pirate-parrot).

One Pirate serves its admitted Ponds with one work folder, transcript store
and its own service grants. Admission grants no service access: follow
[Let a Pirate call a service](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/SERVICES_AND_PIRATES.md#let-a-pirate-call-a-service).
Separate Pirates are the boundary for work that must stay apart.
Continue with [configuration](docs/CONFIGURATION.md)
or [the result](#a-run-and-its-result).

## Updating Pirates

All Pirates on a host run the same code from `/opt/boring-pirates`. That keeps
one maintained runtime, but makes an update a shared interruption: finish or
stop assignments on all affected Pirates first. Read the 0.4.0 notes on the
[releases page](https://github.com/the-boring-crocodile/boring-pirates/releases),
then run the update block in one shell.

    V=0.4.0
    f=boring-pirates-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-pirates/releases/download/v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases &&
    sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-pirates-$V/deploy --check &&
    sudo /root/releases/boring-pirates-$V/deploy

How deploy checks and switches a release:
[boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md#using-a-release).

The first command inspects the release and host without changing them; the
second performs the update. A plain
`deploy` updates it, restarts every Pirate that runs and pings each of its
flavours as live registration-socket owners. Only an admitted worker's ping
can succeed; no admitted live worker means the smoke check fails. A stopped
Pirate stays stopped and is not pinged (all stopped: `site
is stopped: not started`).

### Changed or unrecorded systemd units

A release may need different systemd units as well as new Python code. The
deploy replaces only units it can identify as its own, so it does not silently
discard an administrator's changes.

It renders each Pirate's units again from the new release's templates.
`/etc/boring-pirates/NAME/rendered.json` records the Pirate's account and
groups (install's choices) and what install or a deploy wrote; the other values
come from the host (the account's home, config.json's provider). A unit still
as recorded is replaced just before the switch, the old files kept in
`/etc/boring-pirates/NAME/units-backup-TIMESTAMP/`, and a changed task socket is
restarted; an absent unit is written. A unit that is not what was recorded (a
hand edit) stops the deploy at its check, before any change, with the diff.
Hand edits belong in drop-ins (`UNIT.d/*.conf`), which no deploy touches, nor
config.json: move the edit there, remove the unit file and deploy again.

A record written before 0.3.9 names no account: the deploy stops at its check
and says so. Add `"account"` and `"groups"` (a list) to it, the values of the
`User=` and `SupplementaryGroups=` lines of `boring-pirate@NAME.service`.
A Pirate installed by 0.3.5 or older has no record at all: write one with
`"release": "hand"`, the account and groups, and under `"units"` its four unit
files with their current SHA-256, so the deploy may replace them (CHANGELOG,
0.3.9 upgrade notes). `deploy --check` lists everything an upgrade still has
to change, all at once, before the deploy itself refuses it.
A rollback to 0.3.6rc9 or older leaves the units as the newer release wrote
them (those releases do not render); the CHANGELOG says when a release's
units need restoring from `units-backup-*` for that.

## Removing a Pirate

Use the Pirates HOME from `/etc/boring-pond/homes.json`; the default command is
`sudo /opt/boring-pirates/current/uninstall --name parrot`. Answer its initial
`[y/N]` prompt. It stops the Pirate and its running tasks and removes nothing
unless they are stopped. If the Crocodile still admits it, the Crocodile shows
its removal plan and asks for typed APPLY. Admission removal restarts the
controller and every enabled Pond worker. Other Pirate processes keep running,
but those worker restarts interrupt their Pond connections.

Home, state, credentials and the account stay and are listed. With
`--delete-data`, the Crocodile first shows and revokes this name's `pirate/NAME`
service caller grants and issued tokens, even if admission was removed earlier.
Then home and state are sealed (root's, mode 0700) and the account goes. You can add this flag after an earlier uninstall:
`sudo /opt/boring-pirates/current/uninstall --name parrot --delete-data`.
The retained configuration and account record identify what remains.
Temporary uninstall keeps service grants; re-admitting that name shows them
in the Crocodile's plan. Flavour settings stay for you to review before reusing
a name; the Crocodile owns all service caller grants and roles. Name retirement
needs a Crocodile whose `pirate-remove` supports `--retire`; an older admin refuses before sealing
or account removal.
A failed seal or account removal keeps the account for a retry. Tooling
never deletes the home or data; inspect and remove them yourself when ready.
How deploy, rollback and uninstall work: [boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md).

## Providers

Both providers speak chat completions over HTTP (HTTPX): Mistral's API, and
for openai_compat an operator-configured OpenAI-compatible chat server,
including Ollama. Network routing, authentication and transport
protection belong to that endpoint's deployment.

For a local server, installation accepts --provider openai_compat,
--endpoint http://model-server.example.test:11434/v1 and --model MODEL.
The endpoint is required for openai_compat and refused for Mistral. Without
--key-file, the compatible provider gets a nonempty local credential placeholder;
an existing credential is retained. Servers requiring a real key use --key-file.
Flavours, settings and failure behavior: [CONFIGURATION](docs/CONFIGURATION.md).

## What a flavour can do

Tools are granted per flavour (`allowed_tools`; none if omitted):

- the seven file tools of the Crocodile (`file_list`, `file_search`,
  `file_read`, `file_patch`, `file_write`, `file_read_bytes`,
  `file_write_bytes`), same roots, hash-checked writes and limits, from the
  shared worker kit;
- `exec`, a command in its own systemd unit as the Pirate's account, with the
  same admission, deadline and cleanup as the Crocodile's; one executor per
  Pirate, shared by its flavours;
- `service_list`, `service_describe`, `service_call`: the Pirate authenticates
  to the Crocodile's service authority by its Unix UID and gets a 60-second
  token for one service. Omitted `allowed_services` allows every current grant;
  an explicit list narrows it (`[]`: none). Grant changes need no restart. Command units cannot reach that authority socket; services
  are called through these tools;
- `view_image`, so a vision model can look at an image in its roots. One image
  is attached per turn; another call in that turn is refused before reading it.
  Ask for the next image in a later turn. The configured provider receives the
  original file, including embedded metadata such as EXIF/GPS; unlike the
  Crocodile's file_read_image preview, it is not stripped. Only the latest
  stays in context.

A new Pirate's first flavour gets the file tools only. Use flavours to give
one agent different assignments and budgets; use separate Pirates when work
must have separate permissions. Tool lists are what a flavour is pointed at,
not a sandbox: every flavour has the Pirate's Unix
account, and separation between uses means separate Pirates. What every
flavour shares is the real boundary: the Pirate's UID and groups, its units'
systemd sandbox (all but `work/` read-only), its service grants, and the two
things kept even from its file tools, the API key and the transcripts.

A flavour is **fixed** (its handbook is its rules) or, with
`allow_overrides`, **experimental**: a run may then replace the handbook with
its own system text, narrow the tools and services, and set the turns and the
provider settings within the Pirate's `caps` ([CONFIGURATION](docs/CONFIGURATION.md#per-call-overrides)).
A fixed flavour refuses a run with overrides instead of ignoring them.

## Release set and outcomes

[Outcome causes](OUTCOME_CODES.md) defines local additions and the finite save-wrapper causes.

Install the matching Pirates and Crocodile set in one maintenance window;
keep Pirate work stopped until both updates are complete. The Pirate release
declares its Crocodile requirement in `RELEASE.json`. Crocodile's admin supplies the
Pond and admitted-Pirate inventory. Deploy checks the requirement before preparing
or switching the release. Wire-format changes bump the minor version.
Roll back both components together. Unexpected reply envelopes fail with bad_reply.

A socket error is `{"ok":false,"outcome":{...}}`. The outcome is Common's
flat `{status,code,message,...details}`: refused means nothing started or
changed; failed means work started and may have had effects. Model tool
results embed that same object under `refusal` or `failure`; hints and a
service answer kept under `value` remain outside it. Codes name causes,
independently of status. Detail values must be JSON at construction.

## A run and its result

The Crocodile's worker sends `run` over the Pirate's socket (the wire protocol:
boring-crocodile `docs/SERVICES_AND_PIRATES.md`); the Pirate checks the
caller's UID against an accepted live registration backed by the controller's
current admission proof. Removed or replaced registration sockets revoke that
trust; incompatible versions refuse registration. One assignment per flavour at a time
(`busy` otherwise); different flavours run in parallel. The assignment is
plain text; Goal / Input / Output / Acceptance headings help but are optional.

The result:

| Field | |
|---|---|
| `state`, `reason` | `completed`, `incomplete` (turn or output limit, truncated answer) or `timed_out`, and why; `provider_<reason>` when the provider failed (`rate_limited`, `unauthorized`, `unavailable`, `rejected`, `timed_out`, `invalid_response`) |
| `answer` | the model's final text: a **claim** |
| `write_actions`, `write_actions_count` | the first 32 successful file-tool writes (`file_write`, `file_patch`, `file_write_bytes`, a `service_call` answer saved to a file), with root, path, bytes, SHA-256: **evidence** of those, not of everything: changes made through `exec` or a service's own effects are not in it |
| `tool_errors`, `tool_errors_count` | the first 32 failed tool calls |
| `usage` | tokens (input, output, cached), turns, tool calls, finish reason |
| `elapsed_seconds` | wall time |
| `overrides_used`, `system_sha256` | whether the run brought overrides; the hash of the system message the model got |

A completed run can report failed tools, and an answer can claim work that
never happened: compare it with `write_actions`. After a timeout or
disconnect, effects may exist; look before retrying. Each run's transcript is
a JSONL file named by its `job_id` in the Pirate's state folder; history
failure stops a run before its next tool call.

## Development

The package: `boring_pirates/common/` (everything provider-independent: the
model loop, tools, limits, profiles, IPC) and one folder per provider
(boring_pirates/mistral/ and boring_pirates/openai_compat/), each a few
settings and request fields over the one HTTP transport in common/chat.py. A
new provider is a new folder.

`requirements.lock` pins every runtime dependency with hashes for Python 3.12
to 3.14, Pond Common included (`scripts/lock.py --common-wheels <dist>`).
Release artifact: `scripts/build.py`.

```sh
python -m pytest -q tests
python -m mypy
python scripts/check.py --common-wheels <Common dist>   # fresh venv from the lock, both
```

Tests use synthetic model turns and local fixtures. Changes: [CHANGELOG](CHANGELOG.md).

## Licence

Code under the GNU AGPL, version 3 or later; documentation under CC BY-SA
4.0. What you make with the tools is yours. Details and the exact terms:
[COPYING.md](COPYING.md).
