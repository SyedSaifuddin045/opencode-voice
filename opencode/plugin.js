/**
 * OpenCode voice plugin: forwards assistant text to the local Kokoro TTS
 * daemon (tts-agent, 127.0.0.1:8765) as it streams, flushes on session
 * idle, and stops speech when a new prompt is sent.
 *
 * Server diffs full snapshots per (sessionID, partID) so sending the
 * current full text is correct and idempotent.
 */

import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const VOICE_SERVER = process.env.OC_VOICE_SERVER || "http://127.0.0.1:8765";
const TTS_AGENT = process.env.OC_VOICE_AGENT ||
  join(homedir(), ".local", "bin", "tts-agent");

let lastSpawnAttempt = 0;

// module scope: shared across every plugin instance in this process,
// so a user message can never trigger /stop twice (opencode re-emits
// message.updated at turn end, and the factory may run more than once)
const stoppedMessages = new Set();

/** Lazy-start the daemon when OpenCode launches (or when it died). */
async function ensureDaemon() {
  const now = Date.now();
  if (now - lastSpawnAttempt < 30_000) return; // throttle
  lastSpawnAttempt = now;
  try {
    const res = await fetch(VOICE_SERVER + "/health", {
      signal: AbortSignal.timeout(700),
    });
    if (res.ok) return; // already up
  } catch {
    /* down: spawn below */
  }
  try {
    spawn(TTS_AGENT, ["start"], {
      detached: true,
      stdio: "ignore",
      env: process.env,
    }).unref();
  } catch {
    /* shim missing: stay muted */
  }
}

// fire-and-forget; never block OpenCode on TTS
async function send(path, payload) {
  try {
    const res = await fetch(VOICE_SERVER + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(1500),
    });
    return res.ok;
  } catch {
    // daemon down: try to bring it up, stay muted meanwhile
    ensureDaemon();
    return false;
  }
}

export const VoicePlugin = async ({ project, client, $ }) => {
  // opencode launched: bring the daemon up in the background
  ensureDaemon();

  const roleBySession = new Map(); // sessionID -> Map(messageID -> role)
  const lastSent = new Map(); // partID -> full text already sent

  return {
    event: async ({ event }) => {
      // --- track message roles; stop speech on new user prompt -----
      if (event.type === "message.updated") {
        const info = event.properties?.info;
        if (!info?.id) return;
        let byId = roleBySession.get(info.sessionID);
        if (!byId) {
          byId = new Map();
          roleBySession.set(info.sessionID, byId);
        }
        byId.set(info.id, info.role);
        if (info.role === "user" && !stoppedMessages.has(info.id)) {
          // message.updated fires again when the turn completes;
          // only the first sighting is a genuinely new prompt
          stoppedMessages.add(info.id);
          await send("/stop", {
            sessionID: info.sessionID,
            messageID: info.id,
            source: "message.updated",
          });
          lastSent.clear();
        }
        return;
      }

      // --- streaming assistant text ---------------------------------
      if (event.type === "message.part.updated") {
        const part = event.properties?.part;
        if (!part || part.type !== "text") return;
        if (part.synthetic) return;
        const sessionID = part.sessionID || event.properties?.sessionID;
        if (!sessionID) return;
        const text = part.text || "";
        if (!text) return;

        const role = roleBySession.get(sessionID)?.get(part.messageID);
        if (role !== "assistant") return; // user/system text: stay silent

        lastSent.set(part.id, text);
        await send("/stream", { sessionID, partID: part.id, text });
        return;
      }

      // --- flush remaining sentences when turn completes -----------
      if (event.type === "session.idle") {
        const sessionID = event.properties?.sessionID;
        if (!sessionID) return;
        lastSent.clear();
        roleBySession.delete(sessionID);
        await send("/flush", { sessionID });
        return;
        return;
      }

      // --- interrupt speech on new user prompt ---------------------
      if (event.type === "chat.message") {
        const message = event.properties?.message;
        if (!message || message.role !== "user") return;
        if (stoppedMessages.has(message.id)) return;
        stoppedMessages.add(message.id);
        await send("/stop", {
          sessionID: message.sessionID,
          messageID: message.id,
          source: "chat.message",
        });
        lastSent.clear();
        return;
      }
    },
  };
};

export default VoicePlugin;
