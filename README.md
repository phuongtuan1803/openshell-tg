# openshell-tg

Run Codex, Claude Code or OpenCode inside an isolated virtual machine on your own computer, and decide from Telegram which internet addresses they may use.

```
cd ~/code/my-project
openshell-tg up
```

The first time, `up` asks a few questions and sets everything up. After that it opens the agent straight away.

## What you get

- **Your computer stays safe.** The agent works in a separate virtual machine ([OpenShell](https://github.com/NVIDIA/OpenShell) MicroVM). It sees a copy of the project folder, nothing else.
- **No permission prompts in the terminal.** The agent can edit, install and run anything inside its machine.
- **Network access by Telegram.** When the agent needs a new address, a card arrives on your phone: Approve or Reject. No answer in 30 minutes means Reject.
- **One sandbox per folder**, like Vagrant. Each project folder gets its own machine.

## Requirements

- Linux with virtualization enabled (KVM) and systemd 250 or newer
- Python 3.11 or newer, git
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A ChatGPT or Claude subscription, or an API key

OpenShell itself is installed by `openshell-tg up` if it is missing.

## Install

```
pipx install git+https://github.com/phuongtuan1803/openshell-tg.git
```

## Everyday use

| You want to | Run |
| --- | --- |
| Start or continue working | `openshell-tg up` |
| Leave the agent working in the background | press `Ctrl-B`, then `D` |
| Get the agent's commits into your folder | `openshell-tg pull` (branches appear as `otg/<branch>`) |
| See what is going on | `openshell-tg status`, `openshell-tg list` |
| Remove the sandbox | `openshell-tg destroy` |

In Telegram: `/help`, `/list`, `/status`, `/rules`.

If the computer turns off while the agent works, nothing in the sandbox is lost except commands that were running. Run `openshell-tg up` again to continue.

## Configuration

`up` creates two files you can commit; they never contain keys:

```yaml
# openshell-tg.yaml
agent: codex            # codex | claude | opencode
auth: subscription      # subscription | api_key
providers: [github]     # extra OpenShell provider profiles
policy: openshell/policy.yaml
idle_stop: 24h
```

`openshell/policy.yaml` is a normal [OpenShell policy](https://github.com/NVIDIA/OpenShell) file. Use it to pre-approve addresses or to let the agent change only one part of the project.

Keys and tokens are asked for once, typed hidden, and stored encrypted: API keys and GitHub tokens in OpenShell's credential store, the Telegram bot token with `systemd-creds`.

## How it works

See [docs/design.md](docs/design.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
