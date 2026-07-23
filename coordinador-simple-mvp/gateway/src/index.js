import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join } from "node:path";

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

import { sendAgentReply } from "./delivery.js";
import { buildHumanRoster, identitiesOverlap } from "./group_metadata.js";
import { channelMessagePayload, messageIdentityMetadataOf, messageTextOf } from "./message_metadata.js";
import { PersistentQueue } from "./persistent_queue.js";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://backend:8000";
const TRIGGER_WORD = process.env.TRIGGER_WORD ?? "@coordina";
const AUTH_DIR = process.env.WA_AUTH_DIR ?? "/data/wa-auth";
const MAP_FILE = join(AUTH_DIR, "group-sessions.json");
const PENDING_FILE = join(AUTH_DIR, "pending-messages.json");

const MAX_SENDER = envInt("MAX_SENDER", 80);
const MAX_TEXT = envInt("MAX_TEXT", 500);
const API_TIMEOUT_MS = envInt("API_TIMEOUT_MS", 75000);
const CONNECT_TIMEOUT_MS = envInt("WA_CONNECT_TIMEOUT_MS", 60000);
const KEEP_ALIVE_MS = envInt("WA_KEEP_ALIVE_MS", 30000);
const RECONNECT_MIN_MS = envInt("WA_RECONNECT_MIN_MS", 2000);
const RECONNECT_MAX_MS = envInt("WA_RECONNECT_MAX_MS", 60000);
const GROUP_SYNC_INTERVAL_MS = envInt("WA_GROUP_SYNC_INTERVAL_MS", 300000);
const HEARTBEAT_INTERVAL_MS = envInt("GATEWAY_HEARTBEAT_INTERVAL_MS", 600000);
const RECENT_MESSAGE_LIMIT = envInt("RECENT_MESSAGE_LIMIT", 500);
const MAX_PENDING_MESSAGES = envInt("MAX_PENDING_MESSAGES", 500);
const PENDING_RETRY_MIN_MS = envInt("PENDING_RETRY_MIN_MS", 2000);
const PENDING_RETRY_MAX_MS = envInt("PENDING_RETRY_MAX_MS", 300000);
const MAX_PENDING_ATTEMPTS = envInt("MAX_PENDING_ATTEMPTS", 12);
const FIRE_INIT_QUERIES = envBool("WA_FIRE_INIT_QUERIES", false);
const HEALTH_PORT = envInt("HEALTH_PORT", 8080);

const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });
const pendingQueue = new PersistentQueue(PENDING_FILE, MAX_PENDING_MESSAGES);

let groupSessions = loadGroupSessions();
const groupSessionPromises = new Map();
const groupSyncPromises = new Map();
let currentSock = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let starting = false;
let shuttingDown = false;
let connectionState = "starting";
let lastConnectedAt = null;
let lastDisconnectedAt = null;
let lastConnectionReason = null;
let drainingPending = false;
let pendingDrainTimer = null;
let groupSyncTimer = null;
let queueOverflowCount = 0;
let lastQueueErrorAt = null;
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
    const preserved = `${MAP_FILE}.corrupt-${Date.now()}`;
    try {
      renameSync(MAP_FILE, preserved);
    } catch {
      // Si ni siquiera se puede preservar, el create-or-get del backend sigue
      // evitando sesiones duplicadas cuando llegue el siguiente mensaje.
    }
    logger.warn({ err: String(err), preserved }, "mapa de sesiones invalido; se preservo para diagnostico");
  }
  return {};
}

function saveGroupSessions(map) {
  mkdirSync(dirname(MAP_FILE), { recursive: true });
  const temp = `${MAP_FILE}.tmp`;
  writeFileSync(temp, JSON.stringify(map, null, 2), { encoding: "utf8", mode: 0o600 });
  renameSync(temp, MAP_FILE);
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
    group_participant_ids: metadata.participantIds,
    coordinator_ids: metadata.coordinatorIds,
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
    const roster = buildHumanRoster(metadata?.participants, sock.user);
    if (!roster) {
      throw new Error("WhatsApp no entrego la lista de participantes");
    }
    return { groupName, ...roster };
  } catch (err) {
    logger.warn({ groupJid, err: String(err) }, "no pude leer metadata del grupo");
    return null;
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
      const error = new Error(`Backend ${path} respondio ${response.status}: ${detail.slice(0, 200)}`);
      error.status = response.status;
      throw error;
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
  if (!metadata) {
    throw new Error(`No se puede sincronizar ${groupJid} sin metadata valida`);
  }
  await api(`/api/sessions/${sessionId}/channel/config`, {
    method: "PATCH",
    body: JSON.stringify({
      trigger_word: TRIGGER_WORD,
      group_jid: groupJid,
      group_name: metadata.groupName,
      group_participant_count: metadata.participantCount,
      group_participant_ids: metadata.participantIds,
      coordinator_ids: metadata.coordinatorIds
    })
  });
}

async function sessionForGroup(sock, groupJid) {
  const inFlight = groupSessionPromises.get(groupJid);
  if (inFlight) return inFlight;

  const promise = sessionForGroupUnlocked(sock, groupJid).finally(() => {
    if (groupSessionPromises.get(groupJid) === promise) {
      groupSessionPromises.delete(groupJid);
    }
  });
  groupSessionPromises.set(groupJid, promise);
  return promise;
}

async function sessionForGroupUnlocked(sock, groupJid) {
  const existing = groupSessions[groupJid];
  const existingSessionId = sessionIdFromEntry(existing);
  if (existingSessionId) {
    if (typeof existing === "string" || !existing.group_name) {
      const metadata = await readGroupMetadata(sock, groupJid);
      if (metadata) {
        await configureBackendChannel(existingSessionId, groupJid, metadata);
        groupSessions[groupJid] = mapEntry(existingSessionId, groupJid, metadata);
        saveGroupSessions(groupSessions);
        logger.info({ groupJid, sessionId: existingSessionId }, "sesion existente enriquecida con metadata");
      } else {
        logger.warn(
          { groupJid, sessionId: existingSessionId },
          "se conservo el padron existente porque WhatsApp no entrego metadata"
        );
      }
    }
    return existingSessionId;
  }

  const metadata = await readGroupMetadata(sock, groupJid);
  if (!metadata) {
    throw new Error(`No se pudo resolver ${groupJid}: metadata de WhatsApp no disponible`);
  }
  const { session } = await api("/api/channel/groups/resolve", {
    method: "POST",
    body: JSON.stringify({
      group_jid: groupJid,
      group_name: metadata.groupName,
      group_participant_count: metadata.participantCount,
      group_participant_ids: metadata.participantIds,
      coordinator_ids: metadata.coordinatorIds,
      trigger_word: TRIGGER_WORD
    })
  });

  groupSessions[groupJid] = mapEntry(session.id, groupJid, metadata);
  saveGroupSessions(groupSessions);
  logger.info({ groupJid, sessionId: session.id, groupName: metadata.groupName }, "sesion creada para el grupo");
  return session.id;
}

async function syncGroupMetadata(sock, groupJid) {
  const sessionId = sessionIdFromEntry(groupSessions[groupJid]);
  if (!sessionId || !groupJid.endsWith("@g.us")) return false;

  const inFlight = groupSyncPromises.get(groupJid);
  if (inFlight) return inFlight;

  const promise = (async () => {
    const metadata = await readGroupMetadata(sock, groupJid);
    if (!metadata) return false;

    await configureBackendChannel(sessionId, groupJid, metadata);
    groupSessions[groupJid] = mapEntry(sessionId, groupJid, metadata);
    saveGroupSessions(groupSessions);
    return true;
  })().finally(() => {
    if (groupSyncPromises.get(groupJid) === promise) {
      groupSyncPromises.delete(groupJid);
    }
  });
  groupSyncPromises.set(groupJid, promise);
  return promise;
}

async function syncKnownGroups(sock) {
  let updated = 0;
  for (const [groupJid, entry] of Object.entries(groupSessions)) {
    const sessionId = sessionIdFromEntry(entry);
    if (!sessionId || !groupJid.endsWith("@g.us")) continue;

    try {
      if (await syncGroupMetadata(sock, groupJid)) updated += 1;
    } catch (err) {
      logger.error({ groupJid, sessionId, err: String(err) }, "fallo sincronizando metadata de un grupo");
    }
  }

  if (updated > 0) {
    logger.info({ updated }, "sesiones de grupos conocidas sincronizadas con metadata");
  }
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
  connectionState = "connecting";

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
    connectionState = "connected";
    lastConnectedAt = new Date().toISOString();
    lastConnectionReason = null;
    logger.info("Gateway conectado a WhatsApp. Escuchando grupos.");
    syncKnownGroups(sock).catch((err) =>
      logger.error({ err: String(err) }, "fallo sincronizando grupos conocidos")
    );
    drainPendingMessages(sock).catch((err) => logger.error({ err: String(err) }, "fallo drenando cola pendiente"));
  }

  if (connection === "close") {
    if (sock !== currentSock) return;

    const statusCode = lastDisconnect?.error?.output?.statusCode;
    const loggedOut = statusCode === DisconnectReason.loggedOut;
    currentSock = null;
    lastDisconnectedAt = new Date().toISOString();
    lastConnectionReason = `status=${statusCode ?? "desconocido"}`;

    if (loggedOut) {
      connectionState = "logged_out";
      logger.error("Sesion cerrada por WhatsApp. Borra el volumen wa-auth y vuelve a escanear el QR.");
      return;
    }

    connectionState = "reconnecting";
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
      if (recentMessageIds.has(messageId) || pendingQueue.has(messageId)) {
        logger.debug({ jid, messageId }, "mensaje duplicado ignorado");
        continue;
      }

      const text = messageTextOf(message).trim().slice(0, MAX_TEXT);
      if (!text) continue;

      const { senderId, senderAliases, mentionedJids } = messageIdentityMetadataOf(message, sock.user);
      const sender = cleanLabel(message.pushName || senderId || "Participante", "Participante", MAX_SENDER);
      try {
        pendingQueue.enqueue({
          id: messageId,
          jid,
          sender,
          senderId,
          senderAliases,
          mentionedJids,
          text,
          primarySent: false,
          documentSent: false
        });
      } catch (err) {
        queueOverflowCount += 1;
        lastQueueErrorAt = new Date().toISOString();
        throw err;
      }
      logger.debug({ jid, messageId, pendingMessages: pendingQueue.size }, "mensaje guardado en cola persistente");
    } catch (err) {
      logger.error({ err: String(err) }, "error procesando un mensaje del grupo");
    }
  }

  await drainPendingMessages(sock);
}

async function drainPendingMessages(sock) {
  if (drainingPending || shuttingDown || sock !== currentSock || connectionState !== "connected") return;
  drainingPending = true;
  if (pendingDrainTimer) {
    clearTimeout(pendingDrainTimer);
    pendingDrainTimer = null;
  }

  try {
    while (!shuttingDown && sock === currentSock && connectionState === "connected") {
      const item = pendingQueue.nextReady();
      if (!item) break;

      try {
        await processPendingMessage(sock, item);
      } catch (err) {
        const status = Number(err?.status) || null;
        const permanentHttpError =
          status !== null && status >= 400 && status < 500 && ![408, 409, 425, 429].includes(status);
        if (permanentHttpError || item.attempts + 1 >= MAX_PENDING_ATTEMPTS) {
          pendingQueue.moveToDeadLetter(item.id, err);
          lastQueueErrorAt = new Date().toISOString();
          logger.error(
            { err: String(err), status, jid: item.jid, messageId: item.id, attempts: item.attempts + 1 },
            "mensaje movido a dead letter"
          );
          continue;
        }

        const failed = pendingQueue.markFailed(
          item.id,
          err,
          PENDING_RETRY_MIN_MS,
          PENDING_RETRY_MAX_MS
        );
        logger.error(
          {
            err: String(err),
            jid: item.jid,
            messageId: item.id,
            attempts: failed?.attempts,
            retryInMs: failed?.delayMs
          },
          "mensaje conservado para reintento"
        );
        continue;
      }

      pendingQueue.remove(item.id);
      rememberMessage(item.id);
      logger.info({ jid: item.jid, messageId: item.id }, "mensaje procesado y retirado de la cola");
    }
  } finally {
    drainingPending = false;
    schedulePendingDrain(sock);
  }
}

async function processPendingMessage(sock, item) {
  const sessionId = await sessionForGroup(sock, item.jid);
  const result = await api(`/api/sessions/${sessionId}/channel/messages`, {
    method: "POST",
    body: JSON.stringify(channelMessagePayload(item))
  });

  if (result.invoked) {
    if (sock !== currentSock || connectionState !== "connected") {
      throw new Error("WhatsApp se desconecto antes de entregar la respuesta");
    }
    await sendAgentReply({
      sock,
      jid: item.jid,
      result,
      pendingItem: item,
      markProgress: (progress) => pendingQueue.markProgress(item.id, progress),
      logger
    });
  }
}

function schedulePendingDrain(sock) {
  if (pendingDrainTimer || pendingQueue.size === 0 || shuttingDown) return;
  if (sock !== currentSock || connectionState !== "connected") return;

  const delayMs = pendingQueue.millisecondsUntilNext();
  if (delayMs === null) return;
  pendingDrainTimer = setTimeout(() => {
    pendingDrainTimer = null;
    drainPendingMessages(sock).catch((err) => logger.error({ err: String(err) }, "fallo reintentando cola"));
  }, Math.max(50, delayMs));
  pendingDrainTimer.unref();
}

async function handleGroupParticipants(sock, event) {
  if (sock !== currentSock) return;

  try {
    const { id: jid, participants, action } = event ?? {};
    if (!jid || !jid.endsWith("@g.us")) return;

    if (sessionIdFromEntry(groupSessions[jid])) {
      const synced = await syncGroupMetadata(sock, jid);
      if (synced) {
        logger.info({ jid, action }, "padron del grupo sincronizado tras cambio de participantes");
      }
    }

    if (action !== "add") return;

    // Solo saluda cuando el AGREGADO es el propio bot (no cada vez que entra alguien).
    const botWasAdded = (participants ?? []).some((participant) => identitiesOverlap(participant, sock.user));
    if (!botWasAdded) return;

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
    "Si informan por otra persona, selecciónenla como mención real: _@Gabo puede todo el día_.",
    "Usen una sola persona mencionada por mensaje.",
    "",
    `Cuando quieran propuestas, escriban *${TRIGGER_WORD}*.`,
    `Para ver todo lo que entiendo: *${TRIGGER_WORD} ayuda*.`
  ].join("\n");
}

function scheduleReconnect(reason) {
  if (shuttingDown || reconnectTimer) return;

  connectionState = "reconnecting";
  lastConnectionReason = reason;
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

function gatewayStatus() {
  return {
    ok: connectionState === "connected",
    state: connectionState,
    connected: connectionState === "connected",
    starting,
    reconnectAttempt,
    knownGroups: Object.keys(groupSessions).length,
    recentMessageIds: recentMessageIds.size,
    pendingMessages: pendingQueue.size,
    deadLetterMessages: pendingQueue.deadLetterSize,
    queueOverflowCount,
    lastQueueErrorAt,
    lastConnectedAt,
    lastDisconnectedAt,
    lastConnectionReason,
    uptimeSeconds: Math.floor(process.uptime())
  };
}

const healthServer = createServer((request, response) => {
  const status = gatewayStatus();
  const path = request.url?.split("?", 1)[0] ?? "/";
  const live = !shuttingDown;
  const ready = status.connected;

  let statusCode = 404;
  let payload = { ok: false, detail: "not found" };
  if (request.method === "GET" && path === "/livez") {
    statusCode = live ? 200 : 503;
    payload = { ...status, ok: live };
  } else if (request.method === "GET" && path === "/readyz") {
    statusCode = ready ? 200 : 503;
    payload = status;
  } else if (request.method === "GET" && path === "/status") {
    statusCode = 200;
    payload = status;
  }

  response.writeHead(statusCode, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(payload));
});

healthServer.on("error", (err) => {
  logger.fatal({ err: String(err), port: HEALTH_PORT }, "no se pudo iniciar el endpoint de salud");
  process.exit(1);
});

healthServer.listen(HEALTH_PORT, "0.0.0.0", () => {
  logger.info({ port: HEALTH_PORT }, "endpoint interno de salud disponible");
});

process.on("SIGTERM", () => {
  shuttingDown = true;
  connectionState = "stopping";
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (pendingDrainTimer) clearTimeout(pendingDrainTimer);
  if (groupSyncTimer) clearInterval(groupSyncTimer);
  closeCurrentSocket();
  healthServer.close(() => process.exit(0));
});

process.on("SIGINT", () => {
  shuttingDown = true;
  connectionState = "stopping";
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (pendingDrainTimer) clearTimeout(pendingDrainTimer);
  if (groupSyncTimer) clearInterval(groupSyncTimer);
  closeCurrentSocket();
  healthServer.close(() => process.exit(0));
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
      connected: connectionState === "connected",
      state: connectionState,
      knownGroups: Object.keys(groupSessions).length,
      recentMessageIds: recentMessageIds.size,
      pendingMessages: pendingQueue.size,
      deadLetterMessages: pendingQueue.deadLetterSize,
      queueOverflowCount
    },
    "heartbeat gateway"
  );
}, HEARTBEAT_INTERVAL_MS).unref();

groupSyncTimer = setInterval(() => {
  const sock = currentSock;
  if (!sock || connectionState !== "connected" || shuttingDown) return;
  syncKnownGroups(sock).catch((err) =>
    logger.error({ err: String(err) }, "fallo en sincronizacion periodica de grupos")
  );
}, GROUP_SYNC_INTERVAL_MS);
groupSyncTimer.unref();

start().catch((err) => {
  logger.error({ err: String(err) }, "fallo al iniciar el gateway");
  scheduleReconnect("fallo inicial");
});
