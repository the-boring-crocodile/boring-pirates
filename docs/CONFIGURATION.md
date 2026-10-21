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
  "socket": "/run/boring-pirate-NAME/pirate.sock",
  "checkout": "/home/ACCOUNT/work",
  "history_dir": "/var/lib/boring-pirate-NAME",
  "key_file": "/run/credentials/boring-pirate@NAME.service/mistral-api-key",
  "client_uids": [1001],
  "descriptor_file": "/home/ACCOUNT/handbook/descriptor.json",
  "default_timeout_seconds": 120,
  "max_timeout_seconds": 600,
  "retention_seconds": 604800,
  "provider_request_timeout_seconds": 300,
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
  "profiles": {
    "default": {
      "active": true,
      "model": "mistral-small-2603",
      "workdir": ".",
      "context_files": ["/home/ACCOUNT/handbook/INSTRUCTIONS.md"],
      "allowed_tools": ["file_list", "file_patch", "file_read", "file_read_bytes",
                        "file_search", "file_write", "file_write_bytes"],
      "max_turns": 12
    }
  }
}
```

| Part | |
|---|---|
| `provider` | Which provider folder serves this Pirate. One provider per Pirate. |
| `socket`, `checkout`, `history_dir`, `descriptor_file` | Where it listens, works, keeps transcripts and finds its self-description. |
| `key_file` | Where systemd hands the API key to the running unit (`LoadCredential`); the key itself is in `/etc/boring-pirates/NAME/credentials/`, root only. |
| `client_uids` | The Pond workers allowed to give it work; `pirate-add` names them. |
| `service_authority` | The Crocodile's service authority socket and UID, from `pirate-add`. Optional. |
| timeouts, `retention_seconds` | Assignment default and maximum, one provider request, how long transcripts are kept. |
| `execution` | Where its commands run (`exec`): working roots, task socket, units, slice, output limit. Optional; without it no flavour can have `exec`. |
| `profiles` | The flavours, by name. |

## The API key

The provider's key never goes into `config.json`, a Pond or a chat.

| | |
|---|---|
| Kept in | `/etc/boring-pirates/NAME/credentials/PROVIDER-api-key` (for example `mistral-api-key`), owner root, mode 0600, in a folder of mode 0700: not even the Pirate's own account can read it |
| Handed over | by systemd when `boring-pirate@NAME.service` starts (`LoadCredential=`), as `/run/credentials/boring-pirate@NAME.service/PROVIDER-api-key`: the path `key_file` names |
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
| `context_files` | Read at the start of every assignment; their hashes are recorded. |
| `instructions` | Extra text for this flavour. |
| `allowed_tools` | Omitted: no tools. |
| `allowed_services` | Omitted: no services. Narrows the Pirate's grants. |
| `files` | File roots (named, read/write, deny, limits; the Crocodile's file policy). Omitted: one root `work` at `workdir`. No root may equal, contain or sit inside the key's directory, `history_dir`, the state directory or `/run/credentials`. |
| `max_turns` | Default 12. |
| `"mistral": {...}` | The provider's own settings: `max_tokens` (default 8192), `temperature`, `reasoning_effort`, `top_p`, `random_seed`, `stop`, `presence_penalty`, `frequency_penalty`, `safe_prompt`, `service_tier`, `prompt_mode`. Only the provider folder knows these; another provider has its own block. |

## Limits

| | |
|---|---|
| Assignment, answer | 32 KiB each |
| Starting context, whole conversation | 128 KiB, 2 MiB |
| One tool result, IPC frame, history record | 512 KiB, 1 MiB, 4 MiB |
| Files | per flavour's file policy; default 512 KiB per file, 96 KiB output |
| Images (`view_image`) | 768 KiB, 4096 px per side, 12 Mpx |
| Service calls | 30 s, as the Crocodile's |
| Provider requests | `provider_request_timeout_seconds`; retried by the SDK with backoff (1 s to 16 s) for up to 60 s on rate limits and outages |

A limit reached is an outcome (`incomplete`, `timed_out`), not proof of work.
