#!/bin/sh
# opencode-voice installer: config, CLI shim, plugin registration hint.
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== opencode-voice install =="

echo "[1/4] python deps (uv sync)"
if command -v uv >/dev/null 2>&1; then
  (cd "$REPO_DIR" && uv sync)
else
  echo "  uv not found - install it first: https://docs.astral.sh/uv/"
  exit 1
fi

echo "[2/4] config"
mkdir -p "$HOME/.config/opencode-voice"
if [ -f "$HOME/.config/opencode-voice/config.toml" ]; then
  echo "  exists, leaving as-is: ~/.config/opencode-voice/config.toml"
else
  cp "$REPO_DIR/opencode/config.example.toml" \
     "$HOME/.config/opencode-voice/config.toml"
  echo "  wrote ~/.config/opencode-voice/config.toml"
fi

echo "[3/4] CLI shim"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/tts-agent" <<EOF
#!/bin/sh
exec uv run --project "$REPO_DIR" tts-agent "\$@"
EOF
chmod +x "$HOME/.local/bin/tts-agent"
echo "  wrote ~/.local/bin/tts-agent"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "  NOTE: add ~/.local/bin to PATH:"
     echo '        export PATH="$HOME/.local/bin:$PATH"' ;;
esac

echo "[4/4] opencode plugin"
PLUGIN_DIR="$HOME/.config/opencode/plugins/voice"
mkdir -p "$PLUGIN_DIR"
cp "$REPO_DIR/opencode/plugin.js" "$PLUGIN_DIR/plugin.js"
echo "  wrote $PLUGIN_DIR/plugin.js"
echo "  register it in ~/.config/opencode/opencode.json:"
echo '        "plugin": [ ..., "./plugins/voice/plugin.js" ]'

echo
echo "done. the plugin starts the daemon on next OpenCode launch."
echo "quick controls:  tts-agent on | off | toggle | status | test"
