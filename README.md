# opencode-voice

Local-first text-to-speech for [OpenCode](https://opencode.ai) on macOS.
Kokoro-82M runs on Apple Silicon via MLX — no cloud TTS, no text leaves the machine.

OpenCode assistant responses are spoken aloud as they stream, sentence by sentence.
Sending a new prompt interrupts speech immediately.

**Lifecycle**: the plugin starts the daemon in the background when OpenCode
launches; the daemon exits itself after 5 minutes without requests (config:
`behavior.idle_shutdown_s`, 0 = never). Nothing to manage manually.

**Quick toggle** (instant, no model reload):

```sh
tts-agent off      # mute — daemon keeps running, ignores speech
tts-agent on       # unmute
tts-agent toggle   # flip
```

State persists across daemon restarts (`~/.local/state/opencode-voice/enabled`).

## Architecture

```
OpenCode (plugin event hook)          localhost:8765            MLX / speakers
┌──────────────────────────┐   POST   ┌──────────────────────┐   ┌─────────────┐
│ plugins/voice/plugin.js  │ /stream  │ tts-agent daemon     │   │ Kokoro-82M  │
│  message.part.updated ───┼─────────▶│  snapshot diff       │──▶│ synth worker│
│  message.updated (user) ─┼─/stop───▶│  SpeechAdapter       │   │ (single     │
│  session.idle ───────────┼─/flush──▶│  sentence splitter   │◀──│  thread)    │
│  fire-and-forget fetch,  │          │  SpeechQueue ────────┼──▶│ sounddevice │
│  never blocks OpenCode  │          └──────────────────────┘   └─────────────┘
└──────────────────────────┘
```

- **Plugin** forwards the full text snapshot of each assistant part.
  The server diffs snapshots, so sends are idempotent.
  User messages are filtered out by role (`message.updated` role tracking)
  and trigger `/stop` (interrupt) exactly once per message id — opencode
  re-emits `message.updated` at turn end, and the daemon also dedupes by
  messageID, so the assistant's own reply is never cut off.
- **SpeechAdapter** strips markdown/code/URLs, applies pronunciation rules,
  and emits sentence-sized chunks while text streams.
- **Single synth worker** owns the model (MLX is not thread-safe to hammer);
  HTTP handlers never touch the model.
- **Generation counter**: `stop()` bumps a generation; a synth that finishes
  after an interrupt is dropped instead of playing stale audio.
- **Playback**: `sounddevice` (PortAudio) chunked 100 ms writes for fast
  interrupt; falls back to `afplay` if CoreAudio probing fails.
- Server binds **127.0.0.1 only**. Nothing leaves the machine.

## Install

```sh
git clone https://github.com/SyedSaifuddin045/opencode-voice.git
cd opencode-voice
./install.sh                 # deps + config + CLI shim + plugin
```

Then add `"./plugins/voice/plugin.js"` to the `plugin` array in
`~/.config/opencode/opencode.json` (the installer prints the exact line).

<details>
<summary>Manual install</summary>

```sh
# 1. dependencies (Python 3.11+, uv)
cd ~/Programming/opencode-voice
uv sync                      # add UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple if pypi CDN is blocked

# 2. config + CLI shim
mkdir -p ~/.config/opencode-voice ~/.local/bin
cat > ~/.local/bin/tts-agent <<'EOF'
#!/bin/sh
exec uv run --project "$HOME/Programming/opencode-voice" tts-agent "$@"
EOF
chmod +x ~/.local/bin/tts-agent

# 3. register the OpenCode plugin (one line in ~/.config/opencode/opencode.json)
#    "plugin": [ ..., "./plugins/voice/plugin.js" ]
#    (already done if you pulled this setup)

# 4. copy the plugin (or let install.sh do it)
mkdir -p ~/.config/opencode/plugins/voice
cp opencode/plugin.js ~/.config/opencode/plugins/voice/plugin.js

# 5. done — the plugin spawns the daemon on next OpenCode launch
#    (optional always-on: tts-agent install-launchd)
```

</details>

First run downloads the model (~165 MB) from Hugging Face. The model is
loaded when the daemon starts — first speech after launch waits for that
(~3.5 s), later speech is instant.

## CLI

```sh
tts-agent start | stop | restart     # daemon lifecycle
tts-agent on | off | toggle          # quick mute/unmute (state persists)
tts-agent status | health            # JSON status / readiness probe
tts-agent test ["some text"]         # speak now (daemon, or in-process fallback)
tts-agent voices                     # list 54 Kokoro voices
tts-agent benchmark                  # load/warmup/latency/RTF/memory JSON
tts-agent run                        # foreground (what launchd runs)
tts-agent install-launchd | uninstall-launchd
```

## HTTP API (127.0.0.1:8765)

| Endpoint  | Payload                                   | Effect                     |
|-----------|-------------------------------------------|----------------------------|
| `POST /speak`  | `{"text": ...}`                     | one-shot speech            |
| `POST /stream` | `{"sessionID","partID","text"}`     | streaming snapshot (diffed)|
| `POST /flush`  | `{"sessionID"}`                     | speak buffered remainder   |
| `POST /stop`   | `{"messageID"?}`                     | interrupt (idempotent per messageID) |
| `POST /enabled`| `{"enabled": bool}`                  | runtime mute/unmute                  |
| `GET /health`  |                                        | `{"ok","model_loaded"}`    |
| `GET /status`  |                                        | queues, voice, uptime      |
| `POST /shutdown`|                                       | graceful exit              |

## Configuration

`~/.config/opencode-voice/config.toml` (env override: `OC_VOICE_CONFIG`):

```toml
[general]
enabled = true
voice = "af_heart"        # 54 voices: af_* (US F), am_* (US M), bf_*, bm_*, ...
speed = 1.0

[tts]
model = "mlx-community/Kokoro-82M-bf16"

[server]
host = "127.0.0.1"        # non-localhost hosts are rejected
port = 8765

[speech]
min_sentence_chars = 60   # wait for this much before speaking a short sentence
max_sentence_chars = 400  # force-cut longer than this

[behavior]
interrupt_on_new_prompt = true
idle_shutdown_s = 300     # daemon exits after N s without requests (0 = never)

[playback]
backend = "auto"          # auto | sounddevice | afplay
volume = 0.9
```

Logs: `~/.local/state/opencode-voice/launchd.log`,
state: `daemon.pid`, `daemon.log` in the same directory.

## Benchmark (Apple M5, 16 GB, bf16)

```json
{
  "model_load_s": 2.55,
  "warmup_s": 0.17,
  "time_to_first_audio_s": 0.30,
  "real_time_factor": 0.088,
  "batch_rtf": 0.088,
  "peak_rss_mb": 778
}
```

~12× real time — synthesis never falls behind streaming.

## Repo layout

```
src/opencode_voice/   daemon: config, markdown, speech adapter, tts, playback, http server, cli
tests/                83 tests, no model required (fakes throughout)
opencode/plugin.js    OpenCode plugin (streaming + stop-on-prompt)
opencode/config.example.toml
install.sh            deps + config + CLI shim + plugin
```

## Tests

```sh
uv run pytest -q          # 83 tests, no model required (fakes throughout)
```

## Troubleshooting

- **no sound**: `tts-agent status` → is it running, `enabled: true`?
  `tts-agent test` → check printed backend; `tail ~/.local/state/opencode-voice/daemon.log`
- **daemon not starting with opencode**: plugin spawns `~/.local/bin/tts-agent start`
  (detached) on launch and retries when a request fails; check shim exists + executable
- **silent plugin**: daemon must answer `tts-agent health`; plugin degrades to muted mode when down
- **voice changed**: edit `voice` in config.toml, `tts-agent restart` (model stays cached)
- **model not loading**: check network to huggingface.co; `mlx-community/Kokoro-82M-bf16` cached in `~/.cache/huggingface`
