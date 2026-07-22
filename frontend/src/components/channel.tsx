import { useEffect, useRef } from "react";

import type { Session } from "../types";
import { buildStructuredPreview } from "../format";

export function ChannelChat({ session }: { session: Session | null }) {
  const messages = session?.channel_messages ?? [];
  const threadRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const thread = threadRef.current;
    if (thread) {
      thread.scrollTop = thread.scrollHeight;
    }
  }, [messages.length, session?.last_agent_reply]);

  return (
    <div className="phone-frame">
      <div className="phone-header">
        <div>
          <strong>Grupo Proyecto TAVI</strong>
          <span>{session?.channel_config.listening_enabled ? "agente escuchando" : "escucha pausada"}</span>
        </div>
        <span className="trigger-chip">{session?.channel_config.trigger_word ?? "@coordina"}</span>
      </div>

      <div className="chat-thread" ref={threadRef}>
        {messages.length ? (
          messages.map((message) => (
            <article
              className={`chat-bubble ${message.kind === "agent" ? "agent" : "human"} ${
                message.detected_invocation ? "invoked" : ""
              }`}
              key={message.id}
            >
              <span>{message.sender}</span>
              <p>{message.text}</p>
            </article>
          ))
        ) : (
          <div className="chat-empty">
            Carga mensajes o escribe uno. La ultima frase del ejemplo invoca al agente con @coordina.
          </div>
        )}
      </div>
    </div>
  );
}

export function ListeningPipeline({ session }: { session: Session | null }) {
  const invoked = Boolean(session?.channel_messages.some((message) => message.detected_invocation));
  const steps = [
    { label: "Escucha", done: Boolean(session?.channel_config.listening_enabled) },
    { label: "Invocacion", done: invoked },
    { label: "Estructura LLM", done: Boolean(session?.participants.length) },
    { label: "Decision Python", done: Boolean(session?.options.length) },
    { label: "Respuesta", done: Boolean(session?.last_agent_reply) }
  ];

  return (
    <div className="pipeline-box">
      {steps.map((step) => (
        <div className={step.done ? "pipeline-step done" : "pipeline-step"} key={step.label}>
          <span />
          <strong>{step.label}</strong>
        </div>
      ))}
    </div>
  );
}

export function StructuredPreview({ session }: { session: Session | null }) {
  return (
    <div className="structured-preview">
      <div className="preview-heading">
        <h3>Formato estructurado</h3>
        <span>{session?.participants.length ?? 0} participantes</span>
      </div>
      <pre>{JSON.stringify(buildStructuredPreview(session), null, 2)}</pre>
    </div>
  );
}
