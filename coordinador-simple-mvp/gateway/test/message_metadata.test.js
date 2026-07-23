import assert from "node:assert/strict";
import test from "node:test";

import {
  channelMessagePayload,
  messageTextOf,
  mentionedJidsOf,
  messageIdentityMetadataOf,
  senderAliasesOf,
  senderIdOf
} from "../src/message_metadata.js";

test("recoge participant y aliases PN/LID del remitente sin incluir el JID del grupo", () => {
  const message = {
    key: {
      remoteJid: "120363000000000000@g.us",
      participant: "123456:8@lid",
      participantPn: "56911111111:4@c.us",
      participantLid: "123456@lid",
      senderPn: "56911111111@s.whatsapp.net",
      senderLid: "123456:2@lid"
    }
  };

  assert.deepEqual(senderAliasesOf(message), [
    "123456@lid",
    "56911111111@s.whatsapp.net"
  ]);
  assert.equal(senderIdOf(message), "123456@lid");
});

test("usa un alias técnico como sender_id solo cuando participant no viene", () => {
  assert.equal(
    senderIdOf({ key: { senderPn: "56922222222:5@s.whatsapp.net", senderLid: "222000@lid" } }),
    "56922222222@s.whatsapp.net"
  );
  assert.equal(senderIdOf({ key: { remoteJid: "120363000000000000@g.us" } }), "");
});

test("extrae y normaliza solo las menciones explícitas del ContextInfo actual", () => {
  const message = {
    message: {
      extendedTextMessage: {
        text: "@Gabo puede todo el día",
        contextInfo: {
          mentionedJid: [
            "56933333333:9@c.us",
            "333000:4@lid",
            "56933333333@s.whatsapp.net",
            "120363000000000000@g.us",
            "canal@newsletter",
            "sin-dominio"
          ],
          quotedMessage: {
            extendedTextMessage: {
              text: "@OtraPersona",
              contextInfo: { mentionedJid: ["56999999999@s.whatsapp.net"] }
            }
          }
        }
      }
    }
  };

  assert.deepEqual(mentionedJidsOf(message), [
    "56933333333@s.whatsapp.net",
    "333000@lid"
  ]);
});

test("desenvuelve mensajes efímeros y conserva menciones de captions", () => {
  const message = {
    message: {
      ephemeralMessage: {
        message: {
          imageMessage: {
            caption: "@Gabo puede todo el día",
            contextInfo: { mentionedJid: ["56933333333@s.whatsapp.net"] }
          }
        }
      }
    }
  };

  assert.deepEqual(mentionedJidsOf(message), ["56933333333@s.whatsapp.net"]);
  assert.equal(messageTextOf(message), "@Gabo puede todo el día");
});

test("un texto con arroba no se convierte por inferencia en una mención", () => {
  assert.deepEqual(
    mentionedJidsOf({ message: { extendedTextMessage: { text: "@Gabo puede todo el día" } } }),
    []
  );
  assert.deepEqual(mentionedJidsOf({ message: { conversation: "@Gabo puede todo el día" } }), []);
});

test("expone metadata de identidad con listas vacías cuando Baileys no da evidencia", () => {
  assert.deepEqual(messageIdentityMetadataOf({ key: {}, message: null }), {
    senderId: "",
    senderAliases: [],
    mentionedJids: []
  });
});

test("construye el payload aditivo sin cambiar los campos históricos", () => {
  assert.deepEqual(
    channelMessagePayload({
      id: "grupo:autor:mensaje",
      sender: "Nicolás",
      senderId: "111000@lid",
      senderAliases: ["111000:3@lid", "56911111111:7@c.us"],
      mentionedJids: ["56933333333:2@s.whatsapp.net", "333000@lid"],
      text: "@Gabo puede todo el día"
    }),
    {
      sender: "Nicolás",
      sender_id: "111000@lid",
      sender_aliases: ["111000@lid", "56911111111@s.whatsapp.net"],
      mentioned_jids: ["56933333333@s.whatsapp.net", "333000@lid"],
      text: "@Gabo puede todo el día",
      message_id: "grupo:autor:mensaje"
    }
  );
});

test("mantiene compatibles los elementos antiguos de la cola persistente", () => {
  assert.deepEqual(
    channelMessagePayload({
      id: "mensaje-antiguo",
      sender: "Gabriel",
      senderId: "56933333333@s.whatsapp.net",
      text: "yo puedo a las 14"
    }),
    {
      sender: "Gabriel",
      sender_id: "56933333333@s.whatsapp.net",
      sender_aliases: ["56933333333@s.whatsapp.net"],
      mentioned_jids: [],
      text: "yo puedo a las 14",
      message_id: "mensaje-antiguo"
    }
  );
});

test("excluye la menciÃ³n del propio bot y conserva el contacto objetivo", () => {
  const message = {
    message: {
      extendedTextMessage: {
        text: "@Coordina quitar @Gabo",
        contextInfo: {
          mentionedJid: ["bot-lid@lid", "56933333333@s.whatsapp.net"]
        }
      }
    }
  };

  assert.deepEqual(
    mentionedJidsOf(message, { id: "56900000000@s.whatsapp.net", lid: "bot-lid@lid" }),
    ["56933333333@s.whatsapp.net"]
  );
});
