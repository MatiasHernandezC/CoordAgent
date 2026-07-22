import type { Session, TokenUsage } from "./types";

export type TokenTotals = {
  totalTokens: number;
  estimatedCostUsd: number;
};

export function formatStatus(status: Session["status"] | undefined) {
  if (!status) return "sin sesion";
  if (status === "draft") return "borrador";
  if (status === "calculated") return "calculada";
  return "confirmada";
}

export function formatElapsed(ms: number) {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatTokenUsage(usage: TokenUsage) {
  if (usage.cached) return "cache: 0 tokens";
  const ttft = usage.time_to_first_token_ms === null ? "" : `, TTFT ${formatElapsed(usage.time_to_first_token_ms)}`;
  return `${usage.total_tokens} tokens, costo aprox. $${usage.estimated_cost_usd.toFixed(6)}${ttft}`;
}

export function formatTokenTotals(totals: TokenTotals) {
  return `${totals.totalTokens} tokens, $${totals.estimatedCostUsd.toFixed(6)}`;
}

export function calculateTokenTotals(session: Session | null): TokenTotals {
  if (!session) {
    return { totalTokens: 0, estimatedCostUsd: 0 };
  }

  return session.messages.reduce<TokenTotals>(
    (totals, message) => {
      if (!message.token_usage || message.token_usage.cached) {
        return totals;
      }

      return {
        totalTokens: totals.totalTokens + message.token_usage.total_tokens,
        estimatedCostUsd: totals.estimatedCostUsd + message.token_usage.estimated_cost_usd
      };
    },
    { totalTokens: 0, estimatedCostUsd: 0 }
  );
}

export function getHeatLevel(percent: number) {
  if (percent >= 75) return "high";
  if (percent >= 40) return "mid";
  if (percent > 0) return "low";
  return "zero";
}

export function buildStructuredPreview(session: Session | null) {
  if (!session) {
    return {
      canal: "simulado",
      estado: "sin_sesion",
      trigger: "@coordina",
      participantes: [],
      opciones: []
    };
  }

  return {
    objetivo: session.title,
    canal: "grupo_tipo_whatsapp_simulado",
    escucha_activa: session.channel_config.listening_enabled,
    trigger: session.channel_config.trigger_word,
    participantes: session.participants.map((participant) => ({
      nombre: participant.name,
      disponibilidad: participant.availability.map((slot) => `${slot.day} ${slot.start}-${slot.end}`)
    })),
    faltantes: session.missing_info,
    opciones: session.options.slice(0, 3).map((option) => ({
      horario: `${option.day} ${option.start}-${option.end}`,
      cobertura: `${option.coverage_percent}%`,
      asisten: option.available_participants,
      no_calzan: option.unavailable_participants
    })),
    respuesta_agente: session.last_agent_reply
  };
}

export function getErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Ocurrio un error inesperado";
}
