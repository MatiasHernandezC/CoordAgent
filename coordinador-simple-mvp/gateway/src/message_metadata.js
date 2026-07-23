import { extractMessageContent } from "@whiskeysockets/baileys";

import { userIdentityAliases } from "./group_metadata.js";

const SENDER_IDENTITY_FIELDS = [
  "participant",
  "participantPn",
  "participantLid",
  "senderPn",
  "senderLid"
];

function currentMessageContent(message) {
  try {
    return extractMessageContent(message?.message) ?? null;
  } catch {
    // Un mensaje malformado no debe detener el drenaje de la cola. El texto
    // seguirá su ruta habitual, pero sin atribuir menciones dudosas.
    return null;
  }
}

/** Texto o caption del contenido vigente, incluyendo wrappers efímeros. */
export function messageTextOf(message) {
  const content = currentMessageContent(message);
  if (!content || typeof content !== "object") return "";
  return (
    content.conversation ??
    content.extendedTextMessage?.text ??
    content.imageMessage?.caption ??
    content.videoMessage?.caption ??
    ""
  );
}

function currentContextInfos(message) {
  const content = currentMessageContent(message);
  if (!content || typeof content !== "object") return [];

  // Solo se inspeccionan los nodos inmediatos del contenido vigente. No se
  // recorre contextInfo.quotedMessage: sus menciones pertenecen al mensaje
  // citado, no al que estamos procesando.
  return Object.values(content)
    .filter((node) => node && typeof node === "object")
    .map((node) => node.contextInfo)
    .filter((contextInfo) => contextInfo && typeof contextInfo === "object");
}

/** JIDs mencionados explícitamente por WhatsApp en el mensaje actual. */
export function mentionedJidsOf(message, ownIdentity = null) {
  const mentioned = currentContextInfos(message).flatMap((contextInfo) =>
    Array.isArray(contextInfo.mentionedJid) ? contextInfo.mentionedJid : []
  );
  const ownAliases = new Set(userIdentityAliases(ownIdentity));
  return userIdentityAliases(mentioned).filter((jid) => !ownAliases.has(jid));
}

/**
 * Todos los aliases PN/LID que Baileys entregó para el remitente del mensaje.
 * No usa remoteJid porque, en un grupo, ese campo identifica al grupo.
 */
export function senderAliasesOf(message) {
  const key = message?.key;
  if (!key || typeof key !== "object") return [];
  return userIdentityAliases(SENDER_IDENTITY_FIELDS.map((field) => key[field]));
}

/**
 * Conserva como sender_id el participant usado históricamente por el gateway.
 * Si Baileys no lo entrega, aprovecha el primer alias técnico disponible.
 */
export function senderIdOf(message) {
  const [participant] = userIdentityAliases(message?.key?.participant);
  return participant || senderAliasesOf(message)[0] || "";
}

export function messageIdentityMetadataOf(message, ownIdentity = null) {
  return {
    senderId: senderIdOf(message),
    senderAliases: senderAliasesOf(message),
    mentionedJids: mentionedJidsOf(message, ownIdentity)
  };
}

/** Construye el contrato aditivo que se envía al endpoint del backend. */
export function channelMessagePayload(item) {
  const storedSenderAliases = userIdentityAliases(item?.senderAliases ?? []);
  const senderAliases =
    storedSenderAliases.length > 0 ? storedSenderAliases : userIdentityAliases(item?.senderId);
  const senderId = item?.senderId || senderAliases[0] || undefined;

  return {
    sender: item?.sender,
    sender_id: senderId,
    sender_aliases: senderAliases,
    mentioned_jids: userIdentityAliases(item?.mentionedJids ?? []),
    text: item?.text,
    message_id: item?.id
  };
}
