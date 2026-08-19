import assert from "node:assert/strict";
import test from "node:test";

import { sendAgentReply } from "../src/delivery.js";


test("si falla el documento reintenta sin duplicar el texto", async () => {
  const calls = [];
  let failDocument = true;
  const sock = {
    async sendMessage(jid, payload) {
      calls.push({ jid, payload });
      if (payload.document && failDocument) {
        failDocument = false;
        throw new Error("fallo documental simulado");
      }
    }
  };
  const item = { id: "m1", primarySent: false, documentSent: false };
  const result = {
    agent_reply: "Decision confirmada",
    agent_reply_format: "text",
    agent_reply_document: Buffer.from("BEGIN:VCALENDAR").toString("base64"),
    agent_reply_document_name: "evento.ics",
    agent_reply_document_mimetype: "text/calendar"
  };
  const markProgress = (progress) => Object.assign(item, progress);

  await assert.rejects(
    sendAgentReply({ sock, jid: "grupo", result, pendingItem: item, markProgress }),
    /fallo documental/
  );
  assert.equal(item.primarySent, true);
  assert.equal(item.documentSent, false);

  await sendAgentReply({ sock, jid: "grupo", result, pendingItem: item, markProgress });
  assert.equal(item.documentSent, true);
  assert.equal(calls.filter((call) => call.payload.text).length, 1);
  assert.equal(calls.find((call) => call.payload.text).payload.linkPreview, null);
  assert.equal(calls.filter((call) => call.payload.document).length, 2);
});
