# opencode-local-llm

Run a coding agent on a **local LLM** without keeping it in memory all day.

`llama-proxy` sits in front of [llama.cpp](https://github.com/ggml-org/llama.cpp)
and starts `llama-server` only when a request arrives. After a configurable
idle timeout it kills the server and releases RAM/VRAM again.

```
llama-proxy — on-demand llama.cpp server
├── llama-proxy.py      # the proxy itself (stdlib only)
├── llama-watch         # live terminal monitor
├── agents/low-build.md # OpenCode agent: smallest-diff code executor
├── config-example.json # OpenCode provider config
├── systemd/            # user service unit
└── LICENSE             # MIT
```

## Why

Running a 7–9B model locally keeps ~7 GB of RAM occupied **all the time**,
even when you are not chatting. On a laptop with 16 GB that leaves no room
for a browser, and the fans never stop.

Measured on a 16 GB laptop (AMD Radeon 680M iGPU, 4 GB VRAM in BIOS):

| | Without proxy | With proxy |
|---|---|---|
| RAM in idle | 7.4 GB held | **0** |
| Free RAM after a task | ~1 GB | **4.3 GB** |
| Startup cost | — | **6 s** |

## Requirements

- `llama-server` (package `llama-cpp` on Arch, or a release binary)
- Python 3.9+
- An OpenAI-compatible client — [OpenCode](https://opencode.ai) is used below

## Install

```bash
git clone https://github.com/<you>/opencode-local-llm ~/.local/share/opencode-local-llm
install -Dm755 ~/.local/share/opencode-local-llm/llama-proxy.py ~/.local/bin/llama-proxy.py
install -Dm755 ~/.local/share/opencode-local-llm/llama-watch    ~/.local/bin/llama-watch
```

## Run

```bash
LLAMA_MODEL=~/Models/your-model.gguf llama-proxy.py
```

The proxy listens on `127.0.0.1:8080` and serves an OpenAI-compatible API at
`/v1/chat/completions`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLAMA_MODEL` | *(required)* | path to a `.gguf` file |
| `LLAMA_IDLE` | `30` | seconds of inactivity before the server is killed |
| `LLAMA_PORT` | `8080` | proxy port (what your client connects to) |
| `LLAMA_UP_PORT` | `8081` | `llama-server` port (internal) |
| `LLAMA_ALIAS` | `local` | model name reported to clients |
| `LLAMA_NGL` | `99` | GPU layers to offload (`0` = pure CPU) |
| `LLAMA_CTX` | `8192` | context size |
| `LLAMA_THREADS` | `6` | CPU threads |
| `LLAMA_EXTRA` | — | extra `llama-server` flags, space separated |
| `LLAMA_LOG` | `/tmp/llama-proxy.log` | server log file |

## systemd (user service)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/llama.service ~/.config/systemd/user/
# edit the LLAMA_MODEL line, then:
systemctl --user daemon-reload
systemctl --user enable --now llama
```

Control it with `systemctl --user status llama` and read logs with
`journalctl --user -u llama -f`.

## OpenCode setup

Copy `config-example.json` into `~/.config/opencode/opencode.json`
(merge the `provider` key if the file already exists), then copy the agent:

```bash
mkdir -p ~/.config/opencode/agents
cp agents/low-build.md ~/.config/opencode/agents/
```

Use it with `@low-build <task>` or `Tab` to switch to it as a primary agent.

## Monitor

```bash
llama-watch
```

Shows whether the server runs, the current task, token counts, speed, and
RAM/VRAM usage. Refreshes every 2 seconds.

## How idle detection works

The proxy polls `GET /slots` on the server. Only `is_processing` is trusted —
`llama.cpp` does not reliably reset `id_task` after a request, which leads to
servers never being killed. If `/slots` itself times out (the server is busy
computing), the proxy assumes **busy** and never kills mid-request.

A useful side effect: a single long generation is never interrupted, because
the server keeps reporting `is_processing: true` the whole time.

## Notes

- The proxy talks to the OpenAI-compatible endpoint. `reasoning_content` is
  the field `llama.cpp` uses for chain-of-thought tokens; it is mapped via the
  `compatibility.reasoningField` model option in OpenCode.
- On integrated GPUs, `--n-gpu-layers 99` splits the model between VRAM and
  shared memory. It lowers CPU load but is usually **slower** than pure CPU
  (`LLAMA_NGL=0`) because there is only one memory bus. Measure both.
- `llama-server` does not support systemd socket activation, hence the proxy.

## License

MIT
