const IDENTITY_FIELDS = ["id", "jid", "lid"];
const USER_JID_SUFFIXES = ["@s.whatsapp.net", "@lid"];

/**
 * Normaliza un identificador de usuario sin confundir su PN con su LID.
 * Solo elimina el sufijo de dispositivo y convierte el legado @c.us a PN.
 */
export function normalizeUserJid(value) {
  if (typeof value !== "string") return "";
  const clean = value.trim().toLowerCase();
  const separator = clean.indexOf("@");
  if (separator <= 0) return "";

  const user = clean.slice(0, separator).split(":", 1)[0].split("_", 1)[0];
  const rawServer = clean.slice(separator + 1);
  if (!user || !rawServer) return "";
  const server = rawServer === "c.us" ? "s.whatsapp.net" : rawServer;
  return `${user}@${server}`;
}

/** Devuelve todos los aliases conocidos de una identidad Baileys. */
export function identityAliases(identity) {
  if (Array.isArray(identity)) {
    return [...new Set(identity.flatMap((item) => identityAliases(item)))];
  }
  if (typeof identity === "string") {
    const normalized = normalizeUserJid(identity);
    return normalized ? [normalized] : [];
  }
  if (!identity || typeof identity !== "object") return [];

  return [
    ...new Set(
      IDENTITY_FIELDS.map((field) => normalizeUserJid(identity[field])).filter(Boolean)
    )
  ];
}

/**
 * Limita una identidad a JIDs que representan usuarios de WhatsApp.
 * Evita que un grupo, newsletter u otro destino técnico termine tratado como
 * participante o mención humana.
 */
export function userIdentityAliases(identity) {
  return identityAliases(identity).filter((alias) =>
    USER_JID_SUFFIXES.some((suffix) => alias.endsWith(suffix))
  );
}

export function identitiesOverlap(left, right) {
  const leftAliases = new Set(identityAliases(left));
  return identityAliases(right).some((alias) => leftAliases.has(alias));
}

function canonicalAlias(aliases) {
  return (
    aliases.find((alias) => alias.endsWith("@s.whatsapp.net")) ??
    aliases.find((alias) => alias.endsWith("@lid")) ??
    aliases[0] ??
    ""
  );
}

/**
 * Une registros PN/LID cuando WhatsApp entrega el vínculo en id/jid/lid.
 * Si no existe ningún vínculo entre dos aliases, no intenta adivinar que sean
 * la misma persona.
 */
function participantIdentities(participants) {
  const identities = [];

  for (const participant of participants) {
    const aliases = identityAliases(participant);
    if (aliases.length === 0) continue;

    const overlappingIndexes = [];
    for (let index = 0; index < identities.length; index += 1) {
      if (aliases.some((alias) => identities[index].aliases.has(alias))) {
        overlappingIndexes.push(index);
      }
    }

    if (overlappingIndexes.length === 0) {
      identities.push({
        aliases: new Set(aliases),
        isAdmin: Boolean(participant?.admin || participant?.isAdmin || participant?.isSuperAdmin)
      });
      continue;
    }

    const primaryIndex = overlappingIndexes[0];
    const primary = identities[primaryIndex];
    aliases.forEach((alias) => primary.aliases.add(alias));
    primary.isAdmin ||= Boolean(participant?.admin || participant?.isAdmin || participant?.isSuperAdmin);

    for (const index of overlappingIndexes.slice(1).reverse()) {
      const duplicate = identities[index];
      duplicate.aliases.forEach((alias) => primary.aliases.add(alias));
      primary.isAdmin ||= duplicate.isAdmin;
      identities.splice(index, 1);
    }
  }

  return identities;
}

/**
 * Construye un padrón humano canónico y excluye a la cuenta de Coordina por
 * cualquiera de sus aliases PN/LID, tanto del participante como de sock.user.
 */
export function buildHumanRoster(participants, ownIdentity) {
  if (!Array.isArray(participants)) return null;

  const ownAliases = new Set(identityAliases(ownIdentity));
  if (ownAliases.size === 0) return null;
  if (participants.some((participant) => identityAliases(participant).length === 0)) return null;

  const identities = participantIdentities(participants);
  const ownIsPresent = identities.some((identity) =>
    [...identity.aliases].some((alias) => ownAliases.has(alias))
  );
  if (identities.length > 0 && !ownIsPresent) return null;

  const humans = identities.filter(
    (identity) => ![...identity.aliases].some((alias) => ownAliases.has(alias))
  );
  const participantIds = humans.map((identity) => canonicalAlias([...identity.aliases])).filter(Boolean);
  const coordinatorIds = humans
    .filter((identity) => identity.isAdmin)
    .map((identity) => canonicalAlias([...identity.aliases]))
    .filter(Boolean);

  return {
    participantCount: participantIds.length,
    participantIds: [...new Set(participantIds)],
    coordinatorIds: [...new Set(coordinatorIds)]
  };
}

function cleanRosterName(value) {
  if (typeof value !== "string") return "";
  return value.trim().replace(/\s+/g, " ").slice(0, 120);
}

function fallbackRosterName(identity) {
  const primary = canonicalAlias([...identity.aliases]);
  const [localPart = "nuevo"] = primary.split("@", 1);
  if (primary.endsWith("@s.whatsapp.net") && /^\d+$/.test(localPart)) {
    return `+${localPart}`;
  }
  return `Participante ${localPart.slice(-8) || "nuevo"}`;
}

/** Devuelve el mismo padron con nombres visibles para hidratar el panel. */
export function humanParticipantRoster(participants, ownIdentity) {
  const roster = buildHumanRoster(participants, ownIdentity);
  if (!roster) return null;

  const ownAliases = new Set(identityAliases(ownIdentity));
  const identities = participantIdentities(participants);
  const participantRoster = identities
    .filter((identity) => ![...identity.aliases].some((alias) => ownAliases.has(alias)))
    .map((identity) => {
      const aliases = [...identity.aliases];
      const primary = canonicalAlias(aliases);
      const source = participants.find((participant) =>
        identityAliases(participant).some((alias) => aliases.includes(alias))
      );
      const name = cleanRosterName(
        source?.notify ?? source?.pushName ?? source?.displayName ?? source?.name
      );
      return { id: primary, name: name || fallbackRosterName(identity) };
    })
    .filter((entry) => entry.id);

  return { ...roster, participantRoster };
}

export function humanParticipantCount(participants, ownIdentity) {
  return buildHumanRoster(participants, ownIdentity)?.participantCount ?? null;
}

export function humanParticipantIds(participants, ownIdentity) {
  return buildHumanRoster(participants, ownIdentity)?.participantIds ?? [];
}

export function coordinatorIds(participants, ownIdentity) {
  return buildHumanRoster(participants, ownIdentity)?.coordinatorIds ?? [];
}
