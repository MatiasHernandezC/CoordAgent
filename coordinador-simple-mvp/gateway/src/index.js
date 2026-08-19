import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

import { sendAgentReply } from "./delivery.js";
import { humanParticipantRoster, identitiesOverlap } from "./group_metadata.js";
import { channelMessagePayload, messageIdentityMetadataOf, messageTextOf } from "./message_metadata.js";
import { extractLinkCode, isCoordinaInvocation } from "./linking.js";
import { PersistentQueue } from "./persistent_queue.js";

const require = createRequire(import.meta.url);
const QRCode = require("qrcode-terminal/vendor/QRCode");
const QRErrorCorrectLevel = require("qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel");

const BACKEND_URL = process.env.BACKEND_URL ?? "http://backend:8000";
const GATEWAY_API_TOKEN = process.env.GATEWAY_API_TOKEN ?? "";
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
let currentQrSvg = null;
let botPhoneNumber = null;
const recentMessageIds = new Set();

function qrToSvg(value) {
  const qr = new QRCode(-1, QRErrorCorrectLevel.L);
  qr.addData(value);
  qr.make();

  const quietZone = 4;
  const moduleCount = qr.getModuleCount();
  const size = moduleCount + quietZone * 2;
  const darkModules = [];

  for (let row = 0; row < moduleCount; row += 1) {
    for (let column = 0; column < moduleCount; column += 1) {
      if (qr.modules[row][column]) {
        darkModules.push(`<rect x="${column + quietZone}" y="${row + quietZone}" width="1" height="1"/>`);
      }
    }
  }

  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">`,
    `<rect width="100%" height="100%" fill="white"/>`,
    `<g fill="black">${darkModules.join("")}</g>`,
    "</svg>"
  ].join("");
}

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
    participant_roster: metadata.participantRoster,
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
    const roster = humanParticipantRoster(metadata?.participants, sock.user);
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
      headers: {
        "Content-Type": "application/json",
        ...(GATEWAY_API_TOKEN ? { "X-Coordina-Gateway": GATEWAY_API_TOKEN } : {}),
        ...(options.headers ?? {})
      }
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
      coordinator_ids: metadata.coordinatorIds,
      participant_roster: metadata.participantRoster
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
      participant_roster: metadata.participantRoster,
      trigger_word: TRIGGER_WORD,
      create_if_missing: false
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
    currentQrSvg = qrToSvg(qr);
    logger.info("Escanea este QR con el WhatsApp del numero dedicado en Dispositivos vinculados:");
    qrcode.generate(qr, { small: true });
  }

  if (connection === "open") {
    currentQrSvg = null;
    botPhoneNumber = phoneNumberFromIdentity(sock.user);
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

function phoneNumberFromIdentity(identity) {
  const raw = typeof identity === "string" ? identity : identity?.id;
  if (typeof raw !== "string") return null;
  const [localPart, server] = raw.split("@", 2);
  if (!server || !["s.whatsapp.net", "c.us"].includes(server)) return null;
  const digits = localPart.split(":", 1)[0].replace(/\D/g, "");
  return digits ? `+${digits}` : null;
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
  const linkCode = extractLinkCode(item.text, TRIGGER_WORD);
  if (linkCode) {
    await linkPendingGroup(sock, item, linkCode);
    return;
  }

  let sessionId;
  try {
    sessionId = await sessionForGroup(sock, item.jid);
  } catch (err) {
    if (Number(err?.status) !== 404) throw err;
    if (isCoordinaInvocation(item.text, TRIGGER_WORD)) {
      await sendPlainReply(
        sock,
        item,
        `Este grupo todavía no está vinculado. Crea el grupo desde el panel y envía aquí: *${TRIGGER_WORD} vincular TU_CODIGO*.`
      );
    }
    return;
  }
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

async function linkPendingGroup(sock, item, linkCode) {
  const metadata = await readGroupMetadata(sock, item.jid);
  if (!metadata) throw new Error(`No se pudo leer la metadata de ${item.jid} para vincularlo`);

  try {
    const { session } = await api("/api/channel/groups/link", {
      method: "POST",
      body: JSON.stringify({
        link_code: linkCode,
        group_jid: item.jid,
        group_name: metadata.groupName,
        group_participant_count: metadata.participantCount,
        group_participant_ids: metadata.participantIds,
        coordinator_ids: metadata.coordinatorIds,
        participant_roster: metadata.participantRoster,
        trigger_word: TRIGGER_WORD,
        create_if_missing: false
      })
    });
    groupSessions[item.jid] = mapEntry(session.id, item.jid, metadata);
    saveGroupSessions(groupSessions);
    await sendPlainReply(
      sock,
      item,
      `✅ Grupo vinculado con *${session.title}*. Ya pueden escribir disponibilidades y usar *${TRIGGER_WORD}*.`
    );
    logger.info({ jid: item.jid, sessionId: session.id }, "grupo vinculado por codigo de propietario");
  } catch (err) {
    if (![404, 409].includes(Number(err?.status))) throw err;
    await sendPlainReply(
      sock,
      item,
      `No pude usar ese código. Revisa el código pendiente en tu panel y vuelve a enviar *${TRIGGER_WORD} vincular CODIGO*.`
    );
  }
}

async function sendPlainReply(sock, item, text) {
  if (item.primarySent) return;
  await sock.sendMessage(item.jid, { text, linkPreview: null });
  pendingQueue.markProgress(item.id, { primarySent: true });
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

    let linked = true;
    try {
      await sessionForGroup(sock, jid);
    } catch (err) {
      if (Number(err?.status) !== 404) throw err;
      linked = false;
    }
    await sock.sendMessage(jid, { text: buildWelcomeMessage(linked), linkPreview: null });
    logger.info({ jid }, "bienvenida enviada al grupo");
  } catch (err) {
    logger.error({ err: String(err) }, "error enviando la bienvenida al grupo");
  }
}

function buildWelcomeMessage(linked = true) {
  if (!linked) {
    return [
      "*Coordina*",
      "",
      "El bot ya está dentro del grupo, pero todavía falta vincularlo con su propietario.",
      "Abre el panel, crea tu grupo y copia el código mostrado.",
      `Después envía aquí: *${TRIGGER_WORD} vincular TU_CODIGO*.`
    ].join("\n");
  }
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
    botPhoneNumber,
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

  if (request.method === "GET" && path === "/qr") {
    if (!currentQrSvg) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" });
      response.end(status.connected ? "WhatsApp ya esta conectado." : "Esperando un QR nuevo. Recarga en unos segundos.");
      return;
    }
    response.writeHead(200, {
      "Content-Type": "image/svg+xml; charset=utf-8",
      "Cache-Control": "no-store, no-cache, must-revalidate",
      Pragma: "no-cache"
    });
    response.end(currentQrSvg);
    return;
  }

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
