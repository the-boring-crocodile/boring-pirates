# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

## 0.3.10 — 2026-10-31

Upgrades from 0.3.5, as the public upgrade test found them (built on Pond Common 0.7.11):

- `deploy --check` reports every shape the deploy would refuse, all at once and read-only: the
  configuration keys and flavour settings an upgrade removes (`socket`, `history_dir`, `key_file`;
  `context_files`, `instructions`), a missing or pre-0.3.9 `rendered.json`, and each unit file the
  deploy may not replace. The deploy starts with the same list, before it prepares anything;
  before, `--check` passed and the deploy then refused one problem at a time.
- The deploy's refusal for a unit `rendered.json` does not name says what fits a clean older
  install too: name the unit with its SHA-256 and the deploy replaces it (keeping a backup); the
  drop-in advice stays for units that were edited by hand.

Upgrade: as for 0.3.9 (below); `deploy --check` now lists what is left to do. From 0.3.9 nothing
changes on the host; the units render as 0.3.9 wrote them.

## 0.3.9 — 2026-10-30

Everything since 0.3.5. Built on Pond Common 0.7.11.

Providers:

- One transport for every provider: chat completions over HTTP. Mistral no longer uses Mistral's
  SDK (the `mistralai` package and five packages only it needed are gone).
- New provider `openai_compat` for operator-configured OpenAI-compatible chat servers (local
  models): tool calls, model discovery, `provider_endpoint` and installer `--endpoint`.
- Completions are streamed and fail after `provider_idle_timeout_seconds` without data (default
  120); `"stream": false` asks for one answer. Retries on 429, 5xx and lost connections start only
  within the first 60 s of a call; a call's own budget is `provider_request_timeout_seconds`. A run
  out of that budget is reported as `timed_out`. An unusable answer ends the assignment `failed`
  with `provider_invalid_response`.
- A model turn with neither text nor a tool call is asked again, at most three times per
  assignment (`empty_turn_retries` in usage).

Flavours:

- `handbook`: one file (or null) per flavour; it replaces `context_files` and `instructions`,
  which are now refused.
- Per-call `overrides` (`system`, `tools`, `services`, `max_turns`, `settings`) for a flavour
  with `allow_overrides: true`, within its `caps`; refused `forbidden` elsewhere, never ignored.
  Results report `overrides_used` and `system_sha256`.
- Budgets: `max_service_calls`, `write_reminder_after`; `digest_file` appends short rules to every
  model call.
- File roots may contain protected directories (`/proc`, `/sys` and `/dev` too), which are then
  denied; a root inside the checkout is refused.
- `service_call` reads `{"$file": ...}` references through the flavour's roots and saves answers
  with `save_to`, as the Crocodile does; a save that fails reaches the model as a failed tool
  result and stops the rest of that turn's calls.
- Refusals the model sees carry a hint (next step, allowed values, expected arguments); answers
  with a FILES line get a `files_check` against what was written.

Service and deploy:

- Commands no longer reach the key or the transcripts (the exec unit hides them; the Pirate
  process is not dumpable).
- The socket, transcripts and key are where the unit puts them: `$RUNTIME_DIRECTORY/pirate.sock`,
  `$STATE_DIRECTORY`, `$CREDENTIALS_DIRECTORY/PROVIDER-api-key`.
- The deploy renders the Pirates' units from the release's templates and the record
  `/etc/boring-pirates/NAME/rendered.json` (the Pirate's account and groups, and the unit bytes
  it may replace). A hand-edited unit stops the deploy at its check before any change; drop-ins
  and config.json are never touched.
- `boring-pirate check-config` runs as each Pirate in the release check, so a missing key stops a
  deploy before any restart. A service stop ends running assignments with reason "Pirate service
  stopping".

Upgrade, before the deploy (its check refuses the old shapes before any change and names one
problem at a time; nothing changes until it passes; 0.3.10's `deploy --check` lists them all):

- In each `/etc/boring-pirates/NAME/config.json`, remove `socket`, `history_dir` and `key_file`,
  and in each flavour replace `context_files` (and `instructions`) with `handbook`: the one file
  it named (a 0.3.5 install: `"handbook": "/home/ACCOUNT/handbook/INSTRUCTIONS.md"`).
- Each Pirate needs `/etc/boring-pirates/NAME/rendered.json`. A Pirate installed by 0.3.5 has none:
  write `{"release": "hand", "account": ACCOUNT, "groups": [...], "units": {...}}`, account and
  groups from the `User=` and `SupplementaryGroups=` of `boring-pirate@NAME.service`, and in
  `units` each of its four unit files in `/etc/systemd/system` (`boring-pirate@NAME.service`,
  `boring-pirate-task-NAME@.service`, `boring-pirate-task-NAME.socket`,
  `system-pirateNAME.slice`) with its current SHA-256 (`sha256sum FILE`): the deploy then replaces
  them, keeping the old files in `units-backup-*`. A unit the record does not name counts as hand
  edited and stops the deploy. A record written by 0.3.6 to 0.3.8 only gains `account` and
  `groups`.
- Back to 0.3.5: put the old `config.json` back first (0.3.5's check refuses the new shape), then
  `/opt/boring-pirates/previous/deploy`. 0.3.5 ignores `rendered.json` and keeps the units 0.3.9
  wrote.

## 0.3.5 — 2026-10-21

First public release of Pirates, local AI agents with their own Unix account that take assignments from a Pond.
