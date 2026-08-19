function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function extractLinkCode(text, triggerWord = "@coordina") {
  const pattern = new RegExp(
    `^\\s*${escapeRegExp(triggerWord)}\\s+vincular\\s+([a-z0-9]{8,32})\\s*$`,
    "i"
  );
  const match = String(text ?? "").match(pattern);
  return match ? match[1].toUpperCase() : null;
}

export function isCoordinaInvocation(text, triggerWord = "@coordina") {
  return String(text ?? "").trim().toLowerCase().startsWith(triggerWord.trim().toLowerCase());
}
