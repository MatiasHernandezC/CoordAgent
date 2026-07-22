import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  useMultiFileAuthState
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://backend:8000";
const TRIGGER_WORD = process.env.TRIGGER_WORD ?? "@coordina";
const AUTH_DIR = process.env.WA_AUTH_DIR ?? "/data/wa-auth";
const MAP_FILE = join(AUTH_DIR, "group-sessions.json");

const MAX_SENDER = envInt("MAX_SENDER", 80);
const MAX_TEXT = envInt("MAX_TEXT", 500);
const API_TIMEOUT_MS = envInt("API_TIMEOUT_MS", 30000);
const CONNECT_TIMEOUT_MS = envInt("WA_CONNECT_TIMEOUT_MS", 60000);
const KEEP_ALIVE_MS = envInt("WA_KEEP_ALIVE_MS", 30000);
const RECONNECT_MIN_MS = envInt("WA_RECONNECT_MIN_MS", 2000);
const RECONNECT_MAX_MS = envInt("WA_RECONNECT_MAX_MS", 60000);
const GROUP_SYNC_INTERVAL_MS = envInt("WA_GROUP_SYNC_INTERVAL_MS", 300000);
const HEARTBEAT_INTERVAL_MS = envInt("GATEWAY_HEARTBEAT_INTERVAL_MS", 600000);
const RECENT_MESSAGE_LIMIT = envInt("RECENT_MESSAGE_LIMIT", 500);
const FIRE_INIT_QUERIES = envBool("WA_FIRE_INIT_QUERIES", false);

const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });

let groupSessions = loadGroupSessions();
let currentSock = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let starting = false;
let shuttingDown = false;
let lastGroupSyncAt = 0;
const recentMessageIds = new Set();

function envInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function envBool(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  return ["1", "true", "yes", "y", "on"].includes(raw.trim().toLowerCase());
}

function loadGroupSessions() {
  try {
    if (existsSync(MAP_FILE)) {
      const parsed = JSON.parse(readFileSync(MAP_FILE, "utf8"));
      return parsed && typeof parsed === "object" ? parsed : {};
    }
  } catch (err) {
    logger.warn({ err: String(err) }, "no se pudo leer el mapa de sesiones, empiezo vacio");
  }
  return {};
}

function saveGroupSessions(map) {
  mkdirSync(dirname(MAP_FILE), { recursive: true });
  writeFileSync(MAP_FILE, JSON.stringify(map, null, 2));
}

function sessionIdFromEntry(entry) {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object" && typeof entry.session_id === "string") {
    return entry.session_id;
  }
  return "";
}

function mapEntry(sessionId, groupJid, metadata) {
  return {
    session_id: sessionId,
    group_jid: groupJid,
    group_name: metadata.groupName,
    group_participant_count: metadata.participantCount,
    updated_at: new Date().toISOString()
  };
}

function cleanLabel(value, fallback, maxLength = 120) {
  if (typeof value !== "string") return fallback;
  const clean = value.trim().replace(/\s+/g, " ");
  return (clean || fallback).slice(0, maxLength);
}

async function readGroupMetadata(sock, groupJid) {
  try {
    const metadata = await sock.groupMetadata(groupJid);
    const groupName = cleanLabel(metadata?.subject, "grupo de WhatsApp");
    const participantCount = Array.isArray(metadata?.participants) ? metadata.participants.length : null;
    return { groupName, participantCount };
  } catch (err) {
    logger.warn({ groupJid, err: String(err) }, "no pude leer metadata del grupo");
    return { groupName: "grupo de WhatsApp", participantCount: null };
  }
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), API_TIMEOUT_MS);

  try {
    const response = await fetch(`${BACKEND_URL}${path}`, {
      ...options,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(options.headers ?? {}) }
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(`Backend ${path} respondio ${response.status}: ${detail.slice(0, 200)}`);
    }
    return response.json();
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(`Backend ${path} no respondio antes de ${API_TIMEOUT_MS}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

async function configureBackendChannel(sessionId, groupJid, metadata) {
  await api(`/api/sessions/${sessionId}/channel/config`, {
    method: "PATCH",
    body: JSON.stringify({
      listening_enabled: true,
      trigger_word: TRIGGER_WORD,
      group_jid: groupJid,
      group_name: metadata.groupName,
      group_participant_count: metadata.participantCount
    })
  });
}

async function sessionForGroup(sock, groupJid) {
  const existing = groupSessions[groupJid];
  const existingSessionId = sessionIdFromEntry(existing);
  if (existingSessionId) {
    if (typeof existing === "string" || !existing.group_name) {
      const metadata = await readGroupMetadata(sock, groupJid);
      await configureBackendChannel(existingSessionId, groupJid, metadata);
      groupSessions[groupJid] = mapEntry(existingSessionId, groupJid, metadata);
      saveGroupSessions(groupSessions);
      logger.info({ groupJid, sessionId: existingSessionId }, "sesion existente enriquecida con metadata");
    }
    return existingSessionId;
  }

  const metadata = await readGroupMetadata(sock, groupJid);
  const { session } = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title: `WhatsApp - ${metadata.groupName}` })
  });
  await configureBackendChannel(session.id, groupJid, metadata);

  groupSessions[groupJid] = mapEntry(session.id, groupJid, metadata);
  saveGroupSessions(groupSessions);
  logger.info({ groupJid, sessionId: session.id, groupName: metadata.groupName }, "sesion creada para el grupo");
  return session.id;
}

async function syncKnownGroups(sock) {
  const now = Date.now();
  if (now - lastGroupSyncAt < GROUP_SYNC_INTERVAL_MS) return;
  lastGroupSyncAt = now;

  let updated = 0;
  for (const [groupJid, entry] of Object.entries(groupSessions)) {
    const sessionId = sessionIdFromEntry(entry);
    if (!sessionId || !groupJid.endsWith("@g.us")) continue;

    const metadata = await readGroupMetadata(sock, groupJid);
    await configureBackendChannel(sessionId, groupJid, metadata);
    groupSessions[groupJid] = mapEntry(sessionId, groupJid, metadata);
    updated += 1;
  }

  if (updated > 0) {
    saveGroupSessions(groupSessions);
    logger.info({ updated }, "sesiones de grupos conocidas sincronizadas con metadata");
  }
}

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

function messageIdOf(message) {
  return [
    message.key?.remoteJid ?? "",
    message.key?.participant ?? "",
    message.key?.id ?? "",
    message.messageTimestamp ?? ""
  ].join(":");
}

function rememberMessage(messageId) {
  if (!messageId) return false;
  if (recentMessageIds.has(messageId)) return true;

  recentMessageIds.add(messageId);
  if (recentMessageIds.size > RECENT_MESSAGE_LIMIT) {
    const [oldest] = recentMessageIds;
    recentMessageIds.delete(oldest);
  }
  return false;
}

async function start() {
  if (starting || shuttingDown) return;
  starting = true;

  try {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
      version,
      auth: state,
      logger,
      connectTimeoutMs: CONNECT_TIMEOUT_MS,
      keepAliveIntervalMs: KEEP_ALIVE_MS,
      markOnlineOnConnect: true,
      syncFullHistory: false,
      fireInitQueries: FIRE_INIT_QUERIES,
      shouldSyncHistoryMessage: () => false
    });
    currentSock = sock;

    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("connection.update", (update) => handleConnectionUpdate(sock, update));
    sock.ev.on("messages.upsert", (event) => handleMessages(sock, event));
    sock.ev.on("group-participants.update", (event) => handleGroupParticipants(sock, event));
  } finally {
    starting = false;
  }
}

function handleConnectionUpdate(sock, update) {
  const { connection, lastDisconnect, qr } = update;

  if (qr) {
    logger.info("Escanea este QR con el WhatsApp del numero dedicado en Dispositivos vinculados:");
    qrcode.generate(qr, { small: true });
  }

  if (connection === "open") {
    reconnectAttempt = 0;
    logger.info("Gateway conectado a WhatsApp. Escuchando grupos.");
    syncKnownGroups(sock).catch((err) => logger.error({ err: String(err) }, "fallo sincronizando grupos conocidos"));
  }

  if (connection === "close") {
    if (sock !== currentSock) return;

    const statusCode = lastDisconnect?.error?.output?.statusCode;
    const loggedOut = statusCode === DisconnectReason.loggedOut;
    currentSock = null;

    if (loggedOut) {
      logger.error("Sesion cerrada por WhatsApp. Borra el volumen wa-auth y vuelve a escanear el QR.");
      return;
    }

    scheduleReconnect(`conexion cerrada por WhatsApp, status=${statusCode ?? "desconocido"}`);
  }
}

async function handleMessages(sock, { messages, type }) {
  if (sock !== currentSock || type !== "notify") return;

  for (const message of messages) {
    try {
      const jid = message.key.remoteJid ?? "";
      if (!jid.endsWith("@g.us")) continue;
      if (message.key.fromMe) continue;

      const messageId = messageIdOf(message);
      if (rememberMessage(messageId)) {
        logger.debug({ jid, messageId }, "mensaje duplicado ignorado");
        continue;
      }

      const text = textOf(message).trim().slice(0, MAX_TEXT);
      if (!text) continue;

      const sender = cleanLabel(message.pushName || message.key.participant || "Participante", "Participante", MAX_SENDER);
      const sessionId = await sessionForGroup(sock, jid);

      const result = await api(`/api/sessions/${sessionId}/channel/messages`, {
        method: "POST",
        body: JSON.stringify({ sender, text })
      });

      if (result.invoked) {
        await sendAgentReply(sock, jid, result);
      }
    } catch (err) {
      logger.error({ err: String(err) }, "error procesando un mensaje del grupo");
    }
  }
}

async function handleGroupParticipants(sock, event) {
  if (sock !== currentSock) return;

  try {
    const { id: jid, participants, action } = event ?? {};
    if (action !== "add" || !jid || !jid.endsWith("@g.us")) return;

    // Solo saluda cuando el AGREGADO es el propio bot (no cada vez que entra alguien).
    const me = jidNormalizedUser(sock.user?.id ?? "");
    const added = (participants ?? []).map((participant) =>
      jidNormalizedUser(typeof participant === "string" ? participant : participant?.id ?? "")
    );
    if (!me || !added.includes(me)) return;

    await sessionForGroup(sock, jid);
    await sock.sendMessage(jid, { text: buildWelcomeMessage() });
    logger.info({ jid }, "bienvenida enviada al grupo");
  } catch (err) {
    logger.error({ err: String(err) }, "error enviando la bienvenida al grupo");
  }
}

function buildWelcomeMessage() {
  return [
    "*Coordina*",
    "",
    "Hola! Me agregaron para ayudarlos a encontrar horario de reunion.",
    "Escriban su disponibilidad como un mensaje normal, por ejemplo:",
    "_yo puedo lunes en la tarde_",
    "_no me va bien martes de 10 a 12_",
    "",
    `Cuando quieran propuestas, escriban *${TRIGGER_WORD}*.`,
    `Para ver todo lo que entiendo: *${TRIGGER_WORD} ayuda*.`
  ].join("\n");
}

async function sendAgentReply(sock, jid, result) {
  if (result.agent_reply_image) {
    const caption = result.agent_reply_caption || result.agent_reply || "";
    await sock.sendMessage(jid, {
      image: Buffer.from(result.agent_reply_image, "base64"),
      caption
    });
    logger.info({ jid, format: result.agent_reply_format }, "respuesta con imagen enviada al grupo");
  } else if (result.agent_reply) {
    await sock.sendMessage(jid, { text: result.agent_reply });
    logger.info({ jid, format: result.agent_reply_format }, "respuesta de texto enviada al grupo");
  }

  // Documento adjunto (ej. evento .ics al confirmar). Es opcional: si falla el
  // envio, la respuesta de texto ya salio y la coordinacion no se pierde.
  if (result.agent_reply_document && result.agent_reply_document_name) {
    try {
      await sock.sendMessage(jid, {
        document: Buffer.from(result.agent_reply_document, "base64"),
        fileName: result.agent_reply_document_name,
        mimetype: result.agent_reply_document_mimetype || "application/octet-stream"
      });
      logger.info({ jid, fileName: result.agent_reply_document_name }, "documento enviado al grupo");
    } catch (err) {
      logger.error({ jid, err: String(err) }, "no se pudo enviar el documento al grupo");
    }
  }
}

function scheduleReconnect(reason) {
  if (shuttingDown || reconnectTimer) return;

  const delayMs = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** reconnectAttempt);
  reconnectAttempt += 1;
  logger.warn({ reason, delayMs, reconnectAttempt }, "conexion cerrada, reconexion programada");

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    start().catch((err) => {
      logger.error({ err: String(err) }, "fallo al reconectar");
      scheduleReconnect("fallo al iniciar socket");
    });
  }, delayMs);
}

function closeCurrentSocket() {
  try {
    currentSock?.end?.();
  } catch (err) {
    logger.warn({ err: String(err) }, "no se pudo cerrar el socket de WhatsApp limpiamente");
  } finally {
    currentSock = null;
  }
}

process.on("SIGTERM", () => {
  shuttingDown = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  closeCurrentSocket();
  process.exit(0);
});

process.on("SIGINT", () => {
  shuttingDown = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  closeCurrentSocket();
  process.exit(0);
});

process.on("unhandledRejection", (err) => {
  logger.error({ err: String(err) }, "promesa no manejada en gateway");
});

process.on("uncaughtException", (err) => {
  logger.fatal({ err: String(err) }, "excepcion no manejada en gateway");
  process.exit(1);
});

setInterval(() => {
  logger.info(
    {
      connected: Boolean(currentSock),
      knownGroups: Object.keys(groupSessions).length,
      recentMessageIds: recentMessageIds.size
    },
    "heartbeat gateway"
  );
}, HEARTBEAT_INTERVAL_MS).unref();

start().catch((err) => {
  logger.error({ err: String(err) }, "fallo al iniciar el gateway");
  scheduleReconnect("fallo inicial");
});
