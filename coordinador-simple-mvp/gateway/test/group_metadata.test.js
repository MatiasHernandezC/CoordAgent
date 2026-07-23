import assert from "node:assert/strict";
import test from "node:test";

import {
  buildHumanRoster,
  coordinatorIds,
  humanParticipantCount,
  humanParticipantIds,
  identitiesOverlap,
  identityAliases,
  normalizeUserJid,
  userIdentityAliases
} from "../src/group_metadata.js";

test("normaliza dispositivos y el dominio legado sin mezclar PN con LID", () => {
  assert.equal(normalizeUserJid("56911111111:23@c.us"), "56911111111@s.whatsapp.net");
  assert.equal(normalizeUserJid("123456:8@lid"), "123456@lid");
  assert.notEqual(normalizeUserJid("56911111111@s.whatsapp.net"), normalizeUserJid("56911111111@lid"));
});

test("considera id, jid y lid como aliases de una misma identidad", () => {
  assert.deepEqual(
    identityAliases({
      id: "123456:4@lid",
      jid: "56911111111:7@s.whatsapp.net",
      lid: "123456@lid"
    }),
    ["123456@lid", "56911111111@s.whatsapp.net"]
  );
});

test("los aliases humanos descartan grupos, newsletters y valores malformados", () => {
  assert.deepEqual(
    userIdentityAliases([
      "56911111111:4@c.us",
      "111000:2@lid",
      "120363000000000000@g.us",
      "canal@newsletter",
      "sin-dominio"
    ]),
    ["56911111111@s.whatsapp.net", "111000@lid"]
  );
});

test("excluye al bot aunque metadata use LID y sock.user exponga PN y LID", () => {
  const ownIdentity = {
    id: "56999999999:5@s.whatsapp.net",
    lid: "999000:2@lid"
  };
  const participants = [
    { id: "111000@lid", jid: "56911111111@s.whatsapp.net", lid: "111000@lid", admin: "admin" },
    { id: "222000@lid", jid: "56922222222@s.whatsapp.net", lid: "222000@lid", admin: null },
    { id: "999000@lid", jid: "56999999999@s.whatsapp.net", lid: "999000@lid", admin: "admin" }
  ];

  assert.deepEqual(buildHumanRoster(participants, ownIdentity), {
    participantCount: 2,
    participantIds: ["56911111111@s.whatsapp.net", "56922222222@s.whatsapp.net"],
    coordinatorIds: ["56911111111@s.whatsapp.net"]
  });
});

test("une duplicados PN/LID cuando existe un alias que conecta ambos registros", () => {
  const participants = [
    { id: "56911111111@s.whatsapp.net", admin: null },
    { id: "111000@lid", jid: "56911111111@s.whatsapp.net", lid: "111000@lid", admin: "admin" },
    { id: "111000@lid", admin: null },
    { id: "222000@lid", admin: null },
    { id: "bot-lid@lid", jid: "bot@s.whatsapp.net", lid: "bot-lid@lid", admin: "admin" }
  ];

  assert.deepEqual(buildHumanRoster(participants, { id: "bot@s.whatsapp.net", lid: "bot-lid@lid" }), {
    participantCount: 2,
    participantIds: ["56911111111@s.whatsapp.net", "222000@lid"],
    coordinatorIds: ["56911111111@s.whatsapp.net"]
  });
});

test("un grupo que solo contiene al bot tiene exactamente cero humanos", () => {
  const ownIdentity = { id: "56999999999@s.whatsapp.net", lid: "999000@lid" };
  const participants = [
    { id: "999000@lid", jid: "56999999999@s.whatsapp.net", lid: "999000@lid", admin: "admin" }
  ];

  assert.equal(humanParticipantCount(participants, ownIdentity), 0);
  assert.deepEqual(humanParticipantIds(participants, ownIdentity), []);
  assert.deepEqual(coordinatorIds(participants, ownIdentity), []);
});

test("una lista valida vacia conserva cero y no activa ningun fallback", () => {
  assert.deepEqual(buildHumanRoster([], { id: "bot@s.whatsapp.net" }), {
    participantCount: 0,
    participantIds: [],
    coordinatorIds: []
  });
});

test("metadata ausente se distingue de un padron valido con cero humanos", () => {
  assert.equal(buildHumanRoster(undefined, { id: "bot@s.whatsapp.net" }), null);
  assert.equal(buildHumanRoster(null, { id: "bot@s.whatsapp.net" }), null);
  assert.equal(humanParticipantCount(null, { id: "bot@s.whatsapp.net" }), null);
});

test("metadata parcial no reemplaza el padron si no permite identificar al bot", () => {
  assert.equal(
    buildHumanRoster(
      [{ id: "111000@lid", jid: "56911111111@s.whatsapp.net" }],
      { id: "56999999999@s.whatsapp.net", lid: "999000@lid" }
    ),
    null
  );
  assert.equal(
    buildHumanRoster(
      [{ id: "999000@lid", jid: "56999999999@s.whatsapp.net" }, {}],
      { id: "56999999999@s.whatsapp.net", lid: "999000@lid" }
    ),
    null
  );
});

test("detecta que un participante LID agregado es el propio bot", () => {
  assert.equal(
    identitiesOverlap("999000:6@lid", {
      id: "56999999999:3@s.whatsapp.net",
      lid: "999000@lid"
    }),
    true
  );
  assert.equal(
    identitiesOverlap("111000@lid", {
      id: "56999999999@s.whatsapp.net",
      lid: "999000@lid"
    }),
    false
  );
});
