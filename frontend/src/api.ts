import type { LlmKey, LlmKeyListResponse, OpsStatus, ReplyFormat, RuntimeInfo, Session, TokenUsage } from "./types";

const API_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const AUTH_STORAGE_KEY = "coordina.basicAuth";

export type AuthCredentials = {
  username: string;
  password: string;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getSavedAuth(): AuthCredentials | null {
  const encoded = sessionStorage.getItem(AUTH_STORAGE_KEY);
  if (!encoded) return null;

  try {
    return JSON.parse(atob(encoded)) as AuthCredentials;
  } catch {
    sessionStorage.removeItem(AUTH_STORAGE_KEY);
    return null;
  }
}

export function saveAuth(credentials: AuthCredentials) {
  sessionStorage.setItem(AUTH_STORAGE_KEY, btoa(JSON.stringify(credentials)));
}

export function clearAuth() {
  sessionStorage.removeItem(AUTH_STORAGE_KEY);
}

function basicAuthHeader(credentials: AuthCredentials | null = getSavedAuth()) {
  if (!credentials) return null;
  const token = btoa(`${credentials.username}:${credentials.password}`);
  return `Basic ${token}`;
}

async function request<T>(path: string, init: RequestInit = {}, credentials?: AuthCredentials | null): Promise<T> {
  let response: Response;
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");

  const authorization = basicAuthHeader(credentials);
  if (authorization) {
    headers.set("Authorization", authorization);
  }

  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers
    });
  } catch {
    throw new Error(`No pude conectar con el backend en ${API_URL}. Revisa que FastAPI este corriendo.`);
  }

  if (response.status === 401) {
    throw new ApiError(response.status, "Credenciales invalidas o sesion expirada.");
  }

  if (!response.ok) {
    const error = await response.json().catch(() => undefined);
    const detail = typeof error?.detail === "string" ? error.detail : "No se pudo completar la solicitud";
    throw new ApiError(response.status, detail);
  }

  return response.json() as Promise<T>;
}

export function createSession(title: string) {
  return request<{ session: Session }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title })
  });
}

export function listSessions(credentials?: AuthCredentials | null) {
  return request<{ sessions: Session[] }>(
    "/api/sessions",
    {
      method: "GET"
    },
    credentials
  );
}

export function getSession(sessionId: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}`, {
    method: "GET"
  });
}

export function getRuntime() {
  return request<RuntimeInfo>("/api/runtime", {
    method: "GET"
  });
}

export function getOpsStatus() {
  return request<OpsStatus>("/api/ops/status", {
    method: "GET"
  });
}

export function listLlmKeys() {
  return request<LlmKeyListResponse>("/api/admin/llm-keys", { method: "GET" });
}

export function createLlmKey(name: string, secret: string) {
  return request<{ key: LlmKey }>("/api/admin/llm-keys", {
    method: "POST",
    body: JSON.stringify({ name, secret })
  });
}

export function updateLlmKey(
  credentialId: string,
  updates: { name?: string; priority?: number; enabled?: boolean }
) {
  return request<{ key: LlmKey }>(`/api/admin/llm-keys/${credentialId}`, {
    method: "PATCH",
    body: JSON.stringify(updates)
  });
}

export function testLlmKey(credentialId: string) {
  return request<{ ok: boolean; key: LlmKey }>(`/api/admin/llm-keys/${credentialId}/test`, {
    method: "POST"
  });
}

export function deleteLlmKey(credentialId: string, confirmName: string) {
  return request<{ deleted: boolean }>(`/api/admin/llm-keys/${credentialId}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm_name: confirmName })
  });
}

export function validateLogin(credentials: AuthCredentials) {
  return request<RuntimeInfo>(
    "/api/runtime",
    {
      method: "GET"
    },
    credentials
  );
}

export function sendMessage(sessionId: string, message: string) {
  return request<{ session: Session; llm_source: string; elapsed_ms: number; token_usage: TokenUsage | null }>(`/api/sessions/${sessionId}/message`, {
    method: "POST",
    body: JSON.stringify({ message })
  });
}

export function addParticipant(sessionId: string, name: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/participants`, {
    method: "POST",
    body: JSON.stringify({ name })
  });
}

export function removeParticipant(sessionId: string, participantName: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/participants/${encodeURIComponent(participantName)}`, {
    method: "DELETE"
  });
}

export function addAvailability(sessionId: string, participantName: string, day: string, start: string, end: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/availability`, {
    method: "POST",
    body: JSON.stringify({
      participant_name: participantName,
      day,
      start,
      end
    })
  });
}

export function calculateOptions(sessionId: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/calculate`, {
    method: "POST"
  });
}

export function confirmOption(sessionId: string, optionId: string, expectedProposalRevision: number) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/confirm`, {
    method: "POST",
    body: JSON.stringify({ option_id: optionId, expected_proposal_revision: expectedProposalRevision })
  });
}

export function cancelDecision(sessionId: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/cancel-decision`, {
    method: "POST"
  });
}

export function archiveSession(sessionId: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/archive`, {
    method: "POST"
  });
}

export function reopenSession(sessionId: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/reopen`, {
    method: "POST"
  });
}

export function exportSession(sessionId: string) {
  return request<{ filename: string; text: string }>(`/api/sessions/${sessionId}/export`, {
    method: "GET"
  });
}

export function exportSessionCsv(sessionId: string) {
  return request<{ filename: string; text: string }>(`/api/sessions/${sessionId}/export.csv`, {
    method: "GET"
  });
}

export function exportCalendar(sessionId: string) {
  return request<{ filename: string; text: string }>(`/api/sessions/${sessionId}/calendar`, {
    method: "GET"
  });
}

export function configureChannel(
  sessionId: string,
  settings: {
    listening_enabled?: boolean;
    trigger_word?: string;
    workday_start_hour?: number;
    workday_end_hour?: number;
  }
) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/channel/config`, {
    method: "PATCH",
    body: JSON.stringify(settings)
  });
}

export function updateReplyFormat(session: Session, replyFormat: ReplyFormat) {
  return request<{ session: Session }>(`/api/sessions/${session.id}/channel/config`, {
    method: "PATCH",
    body: JSON.stringify({
      listening_enabled: session.channel_config.listening_enabled,
      trigger_word: session.channel_config.trigger_word,
      reply_format: replyFormat
    })
  });
}

export function sendChannelMessage(sessionId: string, sender: string, text: string) {
  return request<{
    session: Session;
    invoked: boolean;
    llm_source: string | null;
    agent_reply: string | null;
    elapsed_ms: number;
    token_usage: TokenUsage | null;
    duplicate: boolean;
  }>(
    `/api/sessions/${sessionId}/channel/messages`,
    {
      method: "POST",
      body: JSON.stringify({ sender, text })
    }
  );
}

export function sendChannelBatch(sessionId: string, messages: Array<{ sender: string; text: string }>) {
  return request<{
    session: Session;
    invoked: boolean;
    llm_source: string | null;
    agent_reply: string | null;
    elapsed_ms: number;
    token_usage: TokenUsage | null;
    duplicate: boolean;
  }>(`/api/sessions/${sessionId}/channel/batch`, {
    method: "POST",
    body: JSON.stringify({ messages })
  });
}
