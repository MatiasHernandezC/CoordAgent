import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

// ── Configuracion (todo por variables de entorno) ──────────────────────────
const BACKEND_URL = process.env.BACKEND_URL ?? "http://backend:8000";
const TRIGGER_WORD = process.env.TRIGGER_WORD ?? "@coordina";
const AUTH_DIR = process.env.WA_AUTH_DIR ?? "/data/wa-auth";
const MAP_FILE = join(AUTH_DIR, "group-sessions.json");
// Limites del backend (ChannelMessageRequest): sender<=80, text<=500.
const MAX_SENDER = 80;
const MAX_TEXT = 500;

const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });

// ── Mapa grupo WhatsApp -> sesion del backend (persistido para sobrevivir reinicios) ──
function loadGroupSessions() {
  try {
    if (existsSync(MAP_FILE)) return JSON.parse(readFileSync(MAP_FILE, "utf8"));
  } catch (err) {
    logger.warn({ err: String(err) }, "no se pudo leer el mapa de sesiones, empiezo vacio");
  }
  return {};
}

function saveGroupSessions(map) {
  mkdirSync(dirname(MAP_FILE), { recursive: true });
  writeFileSync(MAP_FILE, JSON.stringify(map, null, 2));
}

let groupSessions = loadGroupSessions();

// ── Cliente HTTP minimo contra el backend ──────────────────────────────────
async function api(path, options = {}) {
  const response = await fetch(`${BACKEND_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) }
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`Backend ${path} respondio ${response.status}: ${detail.slice(0, 200)}`);
  }
  return response.json();
}

async function sessionForGroup(groupJid) {
  const existing = groupSessions[groupJid];
  if (existing) return existing;

  const { session } = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title: "Coordinacion WhatsApp" })
  });
  await api(`/api/sessions/${session.id}/channel/config`, {
    method: "PATCH",
    body: JSON.stringify({ listening_enabled: true, trigger_word: TRIGGER_WORD })
  });

  groupSessions[groupJid] = session.id;
  saveGroupSessions(groupSessions);
  logger.info({ groupJid, sessionId: session.id }, "sesion creada para el grupo");
  return session.id;
}

// ── Extraer texto plano de un mensaje de WhatsApp ───────────────────────────
function textOf(message) {
  const content = message.message;
  if (!content) return "";
  return (
    content.conversation ??
    content.extendedTextMessage?.text ??
    content.imageMessage?.caption ??
    content.videoMessage?.caption ??
    ""
  );
}

// ── Bucle principal del gateway ─────────────────────────────────────────────
async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({ version, auth: state, logger });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      logger.info("Escanea este QR con el WhatsApp del numero dedicado (Dispositivos vinculados):");
      qrcode.generate(qr, { small: true });
    }

    if (connection === "open") {
      logger.info("Gateway conectado a WhatsApp. Escuchando grupos.");
    }

    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      if (loggedOut) {
        logger.error("Sesion cerrada por WhatsApp. Borra el volumen wa-auth y vuelve a escanear el QR.");
        return;
      }
      logger.warn({ statusCode }, "conexion cerrada, reconectando...");
      start().catch((err) => logger.error({ err: String(err) }, "fallo al reconectar"));
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;

    for (const message of messages) {
      try {
        const jid = message.key.remoteJid ?? "";
        if (!jid.endsWith("@g.us")) continue; // solo mensajes de grupos
        if (message.key.fromMe) continue; // ignora lo que envia el propio agente

        const text = textOf(message).trim().slice(0, MAX_TEXT);
        if (!text) continue;

        const sender = (message.pushName || message.key.participant || "Participante").slice(0, MAX_SENDER);
        const sessionId = await sessionForGroup(jid);

        const result = await api(`/api/sessions/${sessionId}/channel/messages`, {
          method: "POST",
          body: JSON.stringify({ sender, text })
        });

        // El backend decide si la palabra de invocacion disparo una respuesta.
        if (result.invoked && result.agent_reply) {
          await sock.sendMessage(jid, { text: result.agent_reply });
          logger.info({ jid }, "respuesta del agente enviada al grupo");
        }
      } catch (err) {
        logger.error({ err: String(err) }, "error procesando un mensaje del grupo");
      }
    }
  });
}

start().catch((err) => {
  logger.error({ err: String(err) }, "fallo al iniciar el gateway");
  process.exit(1);
});
