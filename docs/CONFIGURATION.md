# Configuring a Pirate

One file per Pirate, `/etc/boring-pirates/NAME/config.json`, written by the
install and owned by the administrator from then on. The Pirate reads it at
start: restart `boring-pirate@NAME` after a change (an active assignment is
interrupted). `pirate_describe` shows a flavour's effective configuration.
The file holds no secret.

## The file

```json
{
  "provider": "mistral",
  "checkout": "/home/ACCOUNT/work",
  "client_uids": [1001],
  "descriptor_file": "/home/ACCOUNT/handbook/descriptor.json",
  "default_timeout_seconds": 120,
  "max_timeout_seconds": 600,
  "retention_seconds": 604800,
  "provider_request_timeout_seconds": 300,
  "provider_idle_timeout_seconds": 120,
  "execution": {
    "name": "NAME",
    "default_cwd": "/home/ACCOUNT/work",
    "allowed_roots": ["/home/ACCOUNT/work"],
    "task_socket_path": "/run/boring-pirate-tasks/NAME.sock",
    "task_unit_prefix": "boring-pirate-task-NAME",
    "task_cgroup_parent": "/system.slice/system-pirateNAME.slice",
    "output_limit_bytes": 65536
  },
  "service_authority": {"...": "from pirate-add"},
  "caps": {"max_turns": 60, "max_output_tokens": 8192},
  "profiles": {
    "default": {
      "active": true,
      "model": "mistral-small-2603",
      "workdir": ".",
      "handbook": "/home/ACCOUNT/handbook/INSTRUCTIONS.md",
      "allowed_tools": ["file_list", "file_patch", "file_read", "file_read_bytes",
                        "file_search", "file_write", "file_write_bytes"],
      "max_turns": 12
    }
  }
}
```

| Part | |
|---|---|
| `provider` | mistral or openai_compat. One provider per Pirate. |
| `provider_endpoint` | HTTP(S) API base URL, required for openai_compat; refused for mistral. Routing/access policy belongs to the deployment. |
| `checkout`, `descriptor_file` | Where it works and finds its self-description. |
| `client_uids` | The Pond workers allowed to give it work; `pirate-add` names them. |
| `service_authority` | The Crocodile's service authority socket and UID, from `pirate-add`. Optional. |
| timeouts, `retention_seconds` | Assignment default and maximum, one provider request, how long transcripts are kept. |
| `execution` | Where its commands run (`exec`): working roots, task socket, units, slice, output limit. Optional; without it no flavour can have `exec`. |
| `caps` | Upper bounds for per-call overrides: `max_turns` (default 60) and `max_output_tokens` (default 8192). Optional. |
| `profiles` | The flavours, by name. |

Where it listens, keeps its transcripts and finds its key is the unit's, not the
file's: systemd makes the directories of `boring-pirate@NAME.service` and names
them to the Pirate. The socket is `$RUNTIME_DIRECTORY/pirate.sock`
(`/run/boring-pirate-NAME/`), the transcripts are in `$STATE_DIRECTORY`
(`/var/lib/boring-pirate-NAME`), the key is `$CREDENTIALS_DIRECTORY/PROVIDER-api-key`
(`LoadCredential=`; the key itself is in `/etc/boring-pirates/NAME/credentials/`,
root only). `socket`, `history_dir` and `key_file` in config.json are refused
(before 0.3.9 they were required: remove them when you upgrade).

## The API key

The provider's key never goes into `config.json`, a Pond or a chat.

| | |
|---|---|
| Kept in | `/etc/boring-pirates/NAME/credentials/PROVIDER-api-key` (for example `mistral-api-key`), owner root, mode 0600, in a folder of mode 0700: not even the Pirate's own account can read it |
| Handed over | by systemd when `boring-pirate@NAME.service` starts (`LoadCredential=`), as `/run/credentials/boring-pirate@NAME.service/PROVIDER-api-key` (`$CREDENTIALS_DIRECTORY`) |
| Read | once, when the Pirate starts; it keeps the key in memory |
| Set | by the installer: typed at the terminal (hidden), or `--key-file FILE`; a key already there is kept, also on reinstall and redeploy |

**Changing the key** (a new key from the provider, or the old one revoked).
As root, on the Pirate's host; replace NAME and PROVIDER (`mistral` unless
`--provider` said otherwise). The key is typed at a hidden prompt, so it never
shows in the terminal, the shell history or a process list; the new file is
written next to the old one, set to 0600 explicitly (a default ACL on the
folder would override the umask) and then moved over it:

```bash
sudo bash -c 'umask 077; read -rsp "New API key: " k; echo; [ -n "$k" ] || exit 1; printf "%s\n" "$k" > /etc/boring-pirates/NAME/credentials/PROVIDER-api-key.new && chmod 600 /etc/boring-pirates/NAME/credentials/PROVIDER-api-key.new && mv /etc/boring-pirates/NAME/credentials/PROVIDER-api-key.new /etc/boring-pirates/NAME/credentials/PROVIDER-api-key'
sudo stat -c '%U %a %n' /etc/boring-pirates/NAME/credentials/PROVIDER-api-key
sudo systemctl restart boring-pirate@NAME.service
```

Expected: `root 600 ...`. The restart fails the assignments running at that
moment (as every restart of a Pirate does), so choose a quiet minute. Then
check from a Pond: `pirate_list` shows the Pirate `available` again; a short
assignment answers instead of ending with `provider_unauthorized`.

An AI working in a Pond cannot do this itself (it needs root, and the key must
not pass through the chat): it prepares the three lines with NAME and
PROVIDER filled in, the person runs them and types the key.

## Flavours

Each entry of `profiles` is one flavour; only `active` ones are offered.

| Key | |
|---|---|
| `active`, `model` | Required. |
| `workdir` | Relative to `checkout`; default `.`. |
| `handbook` | One file, or null: what the model is told first. Read at the start of every assignment; its hash is recorded. It must lie outside the checkout. Never give a flavour a writable file root over it (or over its digest_file): the model could rewrite the rules every later run starts from. exec cannot, its sandbox keeps the home read-only except the work root; the installer makes `handbook/` root's (readable by the Pirate's group). |
| `allow_overrides` | Default false. True: a run may carry `overrides` (below); false: a run with overrides is refused `forbidden`, never run without them. |
| `allowed_tools` | Omitted: no tools. |
| `allowed_services` | Omitted: no services. Narrows the Pirate's grants. |
| `files` | File roots (named, read/write, deny, limits; the Crocodile's file policy). Omitted: one root `work` at `workdir`. Roots may not overlap. A root may be as wide as `/` (read-only, for looking around); the Pirate's UID and its units' sandbox are the boundary. A root strictly inside the checkout is refused (the Pirate can replace folders there, and the root's own path is resolved once, at start), so a flavour with another `workdir` than `.` needs explicit roots. Protected directories: the key's, the state directory (the transcripts), `/run/credentials`, `/proc`, `/sys` and `/dev`. A root equal to or inside one is refused; a root containing one gets it as a `deny` entry automatically. They stay protected because the file tools run inside the Pirate process, which holds the key (its own memory is readable to it under `/proc`) and writes the transcripts. |
| `max_turns` | Default 12. |
| `max_service_calls` | Optional: after this many `service_call`s in one assignment, further ones are refused with a message to write the result. |
| `write_reminder_after` | Optional: after this many tool calls without a file write, one user message asks to write the result now. |
| `digest_file` | Optional: a short text file (at most 4 KiB, outside the checkout) of key rules. On every model call it is appended to the end of the last message (the prompt on the first call, then the latest tool result) and not kept in the conversation, so the rules stay next to the answer in long assignments without reading as a new task. Its sha256 is part of the profile revision. |
| `"mistral": {...}` | The provider's own settings: `max_tokens` (default 8192), `temperature`, `reasoning_effort`, `top_p`, `random_seed`, `stop`, `presence_penalty`, `frequency_penalty`, `safe_prompt`, `service_tier`, `prompt_mode`, and `stream` (default true: streamed, with the idle timeout; false: one answer at the end). Only the provider folder knows these; another provider has its own block. |

## Per-call overrides

A flavour with `allow_overrides: true` takes an optional `overrides` object
with a run (through the Crocodile: `job_start(pirate={..., "overrides": ...})`).
Everything is optional. Tools and services only narrow the flavour's; turns
and output tokens may go above the flavour's own values, up to the Pirate's
`caps`:

| Key | |
|---|---|
| `system` | Text (at most 32 KiB) that replaces the handbook for this run. |
| `tools`, `services` | Subsets of the flavour's `allowed_tools`, `allowed_services`. |
| `max_turns` | 1 to `caps.max_turns`. |
| `settings` | The provider block for this run, merged over the flavour's and checked by the provider folder; `max_tokens` at most `caps.max_output_tokens`. Any key of the block can change (for Mistral also `safe_prompt` and `stream`). |

Unknown keys, a tool or service outside the flavour's and values above the
caps are refused `invalid_request`. The model, the endpoint and the key are the
Pirate's and cannot be overridden, nor can the file roots, the timeouts or the
service grants. With `system` the flavour's `digest_file` is not sent (it
repeats the handbook's rules). The model is told the effective tool policy.
The transcript's `assignment` record keeps the overrides in full (the system
text included) and `system_sha256`; the result reports `overrides_used` and
`system_sha256`. `pirate_describe` shows `allow_overrides` and `caps`.

## Requests to the provider

Every provider is reached the same way: chat completions over HTTP
(`boring_pirates/common/chat.py`), Mistral at `https://api.mistral.ai/v1`,
an OpenAI-compatible server at its `provider_endpoint`. The API key goes in an
`Authorization: Bearer` header.

Completions are streamed. Each has an absolute budget of
provider_request_timeout_seconds, covering HTTP attempts and waits; a local
model may generate for minutes, so set it to the longest answer you allow. A
streamed completion also fails when no data has arrived for
provider_idle_timeout_seconds (default 120, at most the request budget), so a
server that stopped is noticed long before the budget ends. Local servers are
silent while they read a long prompt and while they generate a tool call:
Ollama 0.40.10 sends a whole tool call as one chunk at its end (26 s for about
1500 tokens on an RTX 4070). Set the idle time above the longest of those; for
a 131k context, 300 s. A server that answers a streamed request with
plain JSON is read as JSON. Connection failures and
HTTP 429/500/502/503/504 retry with doubling waits from 1 to 16 seconds, but a new
attempt starts only within the first 60 seconds of the call. After each wait,
the retry window is checked again; when it has closed, the last provider failure
is returned without another request. An attempt admitted within that window may
finish later, within the independent request budget.
Read timeouts and other HTTP errors are not retried. The assignment's own
deadline/cancellation still wins. Failures use the existing provider_* reasons.

At start the Pirate asks once (10 seconds, no retry) about each flavour's model:
Mistral's model card (a model Mistral does not know, cannot call tools while
the flavour allows tools, or cannot see images while it allows view_image is
not offered; a planned retirement is logged as a warning), an
OpenAI-compatible server's model list (below). No answer is no reason to
refuse a flavour.

## OpenAI-compatible chat servers

Use provider openai_compat with provider_endpoint set to the server's API base,
for example http://model-server.example.test:11434/v1. The endpoint accepts
HTTP or HTTPS; embedded credentials, query strings and fragments are refused.
The operator configures reachability and access control. There is no separate
host allow-list in Pirate code.

An openai_compat flavour has an openai_compat block:

```json
{
  "max_tokens": 2048,
  "reasoning_effort": "none"
}
```

Other optional keys are temperature (0–2), top_p (0–1), seed (integer >=0),
stop (nonempty text or list of nonempty texts), reasoning_effort
(none/low/medium/high), keep_reasoning (boolean, default false) and stream
(boolean, default true; false only for a server that cannot stream).
Unknown settings are refused. The server/model must support the requested
settings; OpenAI compatibility is not proof of every API feature.

By default, returned reasoning is removed from conversation messages; only
its character count is recorded in usage/history. With keep_reasoning=true,
the returned reasoning field remains in the message and is sent back on later
turns. This explicit option also stores that text in the transcript.
Missing prompt/completion usage counts default to zero.

Startup asks /models once, for at most10 seconds. A successful list without the
configured model keeps that flavour from being offered. An unreachable server
is a warning: the flavour remains available for a later assignment. The list
does not establish tool/vision capabilities; verify those with actual tasks.

Ollama ignores the local credential placeholder; it does not provide endpoint
authentication. Keep its credential file/systemd handoff so the Pirate's startup
contract is the same for every provider. Local setup:
[Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility).

## Checking a configuration

`boring-pirate check-config --config PATH` reads what `serve` reads before it
listens: the configuration, every active flavour's context and digest files,
the descriptor and the history directory, and refuses as `serve` would. It
prints a report (active flavours, their revisions and context files) or the
first problem, and exits 1 on a problem. It checks as the account that runs
it, so run it as the Pirate, in a unit of its own with the Pirate's key and the
paths of its state and runtime directories (the running Pirate is not touched).
Do not pass StateDirectory= or RuntimeDirectory=: systemd would apply that
unit's mode (0755 by default) to the live state directory, and remove the live
runtime directory, with the Pirate's socket, when the check ends.

```
systemd-run --wait --pipe --quiet --collect -p User=ACCOUNT -p Group=pirate-clients \
  -p Environment=STATE_DIRECTORY=/var/lib/boring-pirate-NAME \
  -p Environment=RUNTIME_DIRECTORY=/run/boring-pirate-NAME \
  -p LoadCredential=PROVIDER-api-key:/etc/boring-pirates/NAME/credentials/PROVIDER-api-key \
  /opt/boring-pirates/current/bin/boring-pirate check-config \
  --config /etc/boring-pirates/NAME/config.json
```

Inside such a unit the key is `$CREDENTIALS_DIRECTORY/PROVIDER-api-key`, and
check-config checks it there (present, not empty); a
missing key file keeps the unit from starting. The deploy's release check runs
exactly this for every Pirate, with the new code, before any restart. Run
without LoadCredential, the report says the key is "absent here".

## Limits

| | |
|---|---|
| Assignment, answer | 32 KiB each |
| Starting context, whole conversation | 128 KiB, 2 MiB |
| One tool result, IPC frame, history record | 512 KiB, 1 MiB, 4 MiB |
| Files | per flavour's file policy; default 512 KiB per file, 96 KiB output |
| Images (`view_image`) | 768 KiB, 4096 px per side, 12 Mpx |
| Service calls | at most 30 s, less when the assignment has less left (`save_to` keeps 5 s for the write); file references at most 16 and 512 KiB per call |
| Provider requests | provider_request_timeout_seconds per completion, provider_idle_timeout_seconds without data while streaming; retries as in [Requests to the provider](#requests-to-the-provider) |

A limit reached is an outcome (`incomplete`, `timed_out`), not proof of work.
