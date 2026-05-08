import type { RuntimeInfo, Session, TokenUsage } from "./types";

const API_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

async function request<T>(path: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers ?? {})
      }
    });
  } catch {
    throw new Error(`No pude conectar con el backend en ${API_URL}. Revisa que FastAPI este corriendo.`);
  }

  if (!response.ok) {
    const error = await response.json().catch(() => undefined);
    throw new Error(error?.detail ?? "No se pudo completar la solicitud");
  }

  return response.json() as Promise<T>;
}

export function createSession(title: string) {
  return request<{ session: Session }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title })
  });
}

export function getRuntime() {
  return request<RuntimeInfo>("/api/runtime", {
    method: "GET"
  });
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

export function confirmOption(sessionId: string, optionId: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/confirm`, {
    method: "POST",
    body: JSON.stringify({ option_id: optionId })
  });
}

export function configureChannel(sessionId: string, listeningEnabled: boolean, triggerWord: string) {
  return request<{ session: Session }>(`/api/sessions/${sessionId}/channel/config`, {
    method: "PATCH",
    body: JSON.stringify({
      listening_enabled: listeningEnabled,
      trigger_word: triggerWord
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
  }>(`/api/sessions/${sessionId}/channel/batch`, {
    method: "POST",
    body: JSON.stringify({ messages })
  });
}
