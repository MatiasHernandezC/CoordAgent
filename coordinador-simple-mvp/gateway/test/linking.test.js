import assert from "node:assert/strict";
import test from "node:test";

import { extractLinkCode, isCoordinaInvocation } from "../src/linking.js";

test("extrae solamente un comando de vinculacion completo", () => {
  assert.equal(extractLinkCode("@coordina vincular ABCD2345XZ"), "ABCD2345XZ");
  assert.equal(extractLinkCode("  @Coordina   VINCULAR abcd2345xz  "), "ABCD2345XZ");
  assert.equal(extractLinkCode("mensaje @coordina vincular ABCD2345XZ"), null);
  assert.equal(extractLinkCode("@coordina vincular corto"), null);
});

test("detecta invocaciones sin confundir mensajes normales", () => {
  assert.equal(isCoordinaInvocation("@coordina ayuda"), true);
  assert.equal(isCoordinaInvocation("yo puedo el lunes"), false);
});
