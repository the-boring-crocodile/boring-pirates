# Boring Pirates

A Pirate is a local AI agent that a Pond can give work to. Each Pirate is one
instance bound to one AI provider (today Mistral), running under its own Unix
account with its own home, handbook and work folder. It serves one or more
**flavours**: configurations of that provider's models with their own
instructions, tools and budgets. A Pond agent hands a flavour an assignment as
a job (`job_start(pirate=...)`) and reads the result when it is done.

Part of the [Pond family](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md);
shared libraries from [Pond Common](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/README.md).
All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

## Why

A Pond's own model is not always the right worker. Some work is cheaper on a
smaller model, some should run in parallel while the conversation carries on,
and some deserves a second, independent pair of eyes. A Pirate is that
worker, with its own account and its own grants — what it can touch is its
own permission, never the calling Pond's. And when it reports back, trust
its recorded writes, not its prose: a model sometimes claims work that never
happened, so the result lists what was actually done.

## Installing a Pirate

A Pirate joins a running Pond that accepts guests (`service_registration_enabled`).
This page continues the example site of boring-crocodile `docs/INSTALL.md`:
the Crocodile at `crocodile-url.example.test` with the Ponds `pond` and
`garden`; here we add the Pirate `parrot`. One command adds a Pirate, also
next to Pirates already there:

    sudo /root/releases/boring-pirates-VERSION/deploy --install --name parrot --ponds all

Optional: `--account` (default `pirate-parrot`), `--display-name`,
`--provider` (default `mistral`), `--model`, `--key-file`, `--groups`.

It creates the account `pirate-parrot` with `/home/pirate-parrot/work` and
`handbook/`, its state folder `/var/lib/boring-pirate-parrot` and its API key
(typed at the terminal unless `--key-file`; kept root-only, changed as
[CONFIGURATION](docs/CONFIGURATION.md#the-api-key) says), has the Crocodile admit it
(`boring-crocodile-admin pirate-add`: both Ponds may now call it), writes
`/etc/boring-pirates/parrot/config.json` with one flavour, `default`, and its
units (`boring-pirate@parrot.service`, task socket, slice), then deploys. In
`pond` or `garden`, `pirate_list` now shows `pirate-parrot` with the flavour `default`. From then on
`config.json` is yours: [CONFIGURATION](docs/CONFIGURATION.md).

A Pond added to the site later is not served by `parrot` until it is offered
it: the Crocodile's `pirate-add` run again (it names the uids), then
`client_uids` in `config.json` gains the new Pond's worker and the Pirate
restarts; it registers with every Pond whose `register.sock` belongs to one of
`client_uids`. The commands: boring-crocodile
[INSTALL](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/INSTALL.md#a-pirate-parrot). `deploy
--install` takes no settings for an installed Pirate.

A Pirate serves every Pond it is added for (`--ponds`) alike: with its own
service grants (not the calling Pond's), one shared `work/` folder and one
transcript store, so what `parrot` writes for `pond` is there when `garden`
asks. `--ponds` is the boundary: Ponds that must not share work get separate
Pirates.

All Pirates on a host run the same code from `/opt/boring-pirates`. A plain
`deploy` updates it, restarts every Pirate that runs and pings each of its
flavours; a stopped Pirate stays stopped and is not pinged (all stopped: `site
is stopped: not started`).
`sudo /opt/boring-pirates/current/uninstall --name parrot [--delete-data]` takes
it out again: it stops the Pirate and its running tasks and removes nothing
unless they are stopped; its home, state and key stay and are listed. With
`--delete-data` the home and state are first sealed (root's, mode 0700) and
then the account goes; a seal that fails keeps the account, and a rerun
finishes an uninstall that stopped part-way. How deploy, rollback and
uninstall work: [boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md).

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
  token for one service; `allowed_services` can narrow the Pirate's grants,
  never widen them;
- `view_image`, so a vision model can look at an image in its roots.

A new Pirate's first flavour gets the file tools only. Tool lists are what a
flavour is pointed at, not a sandbox: every flavour has the Pirate's Unix
account, and separation between uses means separate Pirates.

## A run and its result

The Crocodile's worker sends `run` over the Pirate's socket (the wire protocol:
boring-crocodile `docs/SERVICES_AND_PIRATES.md`); the Pirate checks the
caller's UID against `client_uids`. One assignment per flavour at a time
(`busy` otherwise); different flavours run in parallel. The assignment is
plain text; Goal / Input / Output / Acceptance headings help but are optional.

The result:

| Field | |
|---|---|
| `state`, `reason` | `completed`, `incomplete` (turn or output limit, truncated answer) or `timed_out`, and why; `provider_<reason>` when the provider failed (`rate_limited`, `unauthorized`, `unavailable`, `rejected`, `timed_out`, `invalid_response`) |
| `answer` | the model's final text: a **claim** |
| `write_actions`, `write_actions_count` | the first 32 successful writes, with root, path, bytes, SHA-256: **evidence** |
| `tool_errors`, `tool_errors_count` | the first 32 failed tool calls |
| `usage` | tokens (input, output, cached), turns, tool calls, finish reason |
| `elapsed_seconds` | wall time |

A completed run can report failed tools, and an answer can claim work that
never happened: compare it with `write_actions`. After a timeout or
disconnect, effects may exist; look before retrying. Each run's transcript is
a JSONL file named by its `job_id` in the Pirate's state folder; history
failure stops a run before its next tool call.

## Development

The package: `boring_pirates/common/` (everything provider-independent: the
model loop, tools, limits, profiles, IPC) and one folder per provider
(`boring_pirates/mistral/`, on the official Mistral SDK). A new provider is a
new folder.

`requirements.lock` pins every runtime dependency with hashes for Python 3.10
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
