import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  addAvailability,
  addParticipant,
  calculateOptions,
  configureChannel,
  confirmOption,
  createSession,
  getRuntime,
  sendChannelBatch,
  sendChannelMessage,
  sendMessage
} from "./api";
import type { AvailabilityCell, Day, RuntimeInfo, Session, TokenUsage } from "./types";

const EXAMPLE =
  "Yo puedo lunes en la tarde, Camila puede lunes desde las 16 y Diego puede martes en la manana, Pedro puede a cualquier hora todos los dias";
const CHANNEL_EXAMPLE = [
  { sender: "Nicolas", text: "yo puedo lunes en la tarde" },
  { sender: "Camila", text: "yo puedo lunes desde las 16" },
  { sender: "Diego", text: "yo puedo martes en la manana" },
  { sender: "Pedro", text: "puedo a cualquier hora todos los dias" },
  { sender: "Nicolas", text: "@coordina nos ayudas a cerrar un horario?" }
];

const DAYS: Day[] = ["lunes", "martes", "miercoles", "jueves", "viernes"];
const HOURS = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00"];

export function App() {
  const [title, setTitle] = useState("Reunion grupal");
  const [message, setMessage] = useState(EXAMPLE);
  const [session, setSession] = useState<Session | null>(null);
  const [llmSource, setLlmSource] = useState("");
  const [manualName, setManualName] = useState("");
  const [manualDay, setManualDay] = useState<Day>("lunes");
  const [manualStart, setManualStart] = useState("09:00");
  const [manualEnd, setManualEnd] = useState("18:00");
  const [channelSender, setChannelSender] = useState("Nicolas");
  const [channelText, setChannelText] = useState("yo puedo lunes en la tarde");
  const [triggerWord, setTriggerWord] = useState("@coordina");
  const [listeningEnabled, setListeningEnabled] = useState(true);
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [lastElapsedMs, setLastElapsedMs] = useState<number | null>(null);
  const [lastTokenUsage, setLastTokenUsage] = useState<TokenUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingLabel, setLoadingLabel] = useState("");
  const [error, setError] = useState("");

  const bestOption = session?.options[0] ?? null;
  const tokenTotals = useMemo(() => calculateTokenTotals(session), [session]);
  const totalSlots = useMemo(
    () => session?.participants.reduce((sum, participant) => sum + participant.availability.length, 0) ?? 0,
    [session]
  );

  useEffect(() => {
    getRuntime()
      .then(setRuntime)
      .catch(() => setRuntime(null));
  }, []);

  async function runAction(label: string, action: () => Promise<void>) {
    setLoading(true);
    setLoadingLabel(label);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setLoading(false);
      setLoadingLabel("");
    }
  }

  async function handleCreateSession(event: FormEvent) {
    event.preventDefault();
    await runAction("Creando sesion...", async () => {
      const response = await createSession(title);
      setSession(response.session);
      setLlmSource("");
      setLastTokenUsage(null);
      setTriggerWord(response.session.channel_config.trigger_word);
      setListeningEnabled(response.session.channel_config.listening_enabled);
    });
  }

  async function handleSendMessage(event: FormEvent) {
    event.preventDefault();
    if (!session) return;

    await runAction("Extrayendo disponibilidad con LLM...", async () => {
      const response = await sendMessage(session.id, message);
      setSession(response.session);
      setLlmSource(response.llm_source);
      setLastElapsedMs(response.elapsed_ms);
      setLastTokenUsage(response.token_usage);
    });
  }

  async function handleAddManual(event: FormEvent) {
    event.preventDefault();
    if (!session || !manualName.trim()) return;

    await runAction("Agregando correccion manual...", async () => {
      const participantResponse = await addParticipant(session.id, manualName.trim());
      const availabilityResponse = await addAvailability(
        participantResponse.session.id,
        manualName.trim(),
        manualDay,
        manualStart,
        manualEnd
      );
      setSession(availabilityResponse.session);
      setManualName("");
    });
  }

  async function handleCalculate() {
    if (!session) return;

    await runAction("Calculando mejores horarios...", async () => {
      const response = await calculateOptions(session.id);
      setSession(response.session);
    });
  }

  async function handleConfirm(optionId: string) {
    if (!session) return;

    await runAction("Confirmando opcion...", async () => {
      const response = await confirmOption(session.id, optionId);
      setSession(response.session);
    });
  }

  async function handleConfigureChannel(event: FormEvent) {
    event.preventDefault();
    if (!session) return;

    await runAction("Guardando configuracion del canal...", async () => {
      const response = await configureChannel(session.id, listeningEnabled, triggerWord);
      setSession(response.session);
    });
  }

  async function handleSendChannelMessage(event: FormEvent) {
    event.preventDefault();
    if (!session || !channelSender.trim() || !channelText.trim()) return;

    await runAction("Enviando mensaje al canal...", async () => {
      const response = await sendChannelMessage(session.id, channelSender.trim(), channelText.trim());
      setSession(response.session);
      setLlmSource(response.llm_source ?? llmSource);
      setLastElapsedMs(response.elapsed_ms);
      setLastTokenUsage(response.token_usage);
      setChannelText("");
    });
  }

  async function handleLoadChannelExample() {
    if (!session) return;

    await runAction("Cargando conversacion e invocando al agente...", async () => {
      const response = await sendChannelBatch(session.id, CHANNEL_EXAMPLE);
      setSession(response.session);
      setLlmSource(response.llm_source ?? llmSource);
      setLastElapsedMs(response.elapsed_ms);
      setLastTokenUsage(response.token_usage);
    });
  }

  return (
    <main className="page-shell">
      <section className="app-header">
        <div className="brand-block">
          <div className="brand-row">
            <LogoMark />
            <div>
              <p className="eyebrow">Python + TypeScript + Gemini</p>
              <strong>Coordina AI</strong>
            </div>
          </div>
          <h1>Coordinador inteligente de reuniones</h1>
          <p className="subtitle">
            Escribe disponibilidad en lenguaje natural, revisa lo que el sistema entendio y confirma una opcion explicada.
          </p>
          <div className="hero-tags">
            <span>Canal simulado</span>
            <span>Extraccion LLM</span>
            <span>Decision Python</span>
          </div>
        </div>
        <div className="status-card">
          <span>Estado</span>
          <strong>{formatStatus(session?.status)}</strong>
          <small>{runtime ? `LLM activo: ${runtime.provider_label}` : "LLM activo: revisando..."}</small>
          {lastElapsedMs !== null ? <small>Ultima llamada: {formatElapsed(lastElapsedMs)}</small> : null}
          {lastTokenUsage ? <small>{formatTokenUsage(lastTokenUsage)}</small> : null}
          {tokenTotals.totalTokens > 0 ? <small>Total sesion: {formatTokenTotals(tokenTotals)}</small> : null}
        </div>
      </section>

      {runtime?.warnings.length ? (
        <div className="warning-banner">
          {runtime.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      ) : null}

      {error ? <div className="error-banner">{error}</div> : null}
      {loading ? <div className="activity-banner">{loadingLabel || "Procesando..."}</div> : null}

      <section className="metric-grid">
        <Metric title="Participantes" value={session?.participants.length ?? 0} detail="personas detectadas" />
        <Metric title="Disponibilidades" value={totalSlots} detail="bloques declarados" />
        <Metric title="Faltantes" value={session?.missing_info.length ?? 0} detail="datos por completar" />
        <Metric title="Mejor cobertura" value={bestOption ? `${bestOption.coverage_percent}%` : "-"} detail="opcion principal" />
        <Metric title="Costo Gemini" value={tokenTotals.totalTokens ? `$${tokenTotals.estimatedCostUsd.toFixed(6)}` : "-"} detail={`${tokenTotals.totalTokens} tokens reales`} />
      </section>

      <section className="surface channel-section">
        <div className="section-heading">
          <span className="step-badge">0</span>
          <div>
            <h2>Simulador de canal tipo WhatsApp</h2>
            <p>
              Carga mensajes como si vinieran de un grupo. El agente escucha el canal y responde solo cuando aparece la
              palabra de invocacion.
            </p>
          </div>
        </div>

        <div className="channel-grid">
          <div className="channel-controls">
            <form className="channel-config" onSubmit={handleConfigureChannel}>
              <label>
                Palabra de invocacion
                <input value={triggerWord} onChange={(event) => setTriggerWord(event.target.value)} disabled={!session} />
              </label>
              <label className="switch-row">
                <input
                  type="checkbox"
                  checked={listeningEnabled}
                  onChange={(event) => setListeningEnabled(event.target.checked)}
                  disabled={!session}
                />
                <span>Agente escuchando el canal</span>
              </label>
              <button type="submit" disabled={!session || loading}>
                Guardar escucha
              </button>
            </form>

            <form className="channel-composer" onSubmit={handleSendChannelMessage}>
              <label>
                Remitente
                <input
                  value={channelSender}
                  onChange={(event) => setChannelSender(event.target.value)}
                  disabled={!session}
                />
              </label>
              <label>
                Mensaje al canal
                <textarea
                  value={channelText}
                  onChange={(event) => setChannelText(event.target.value)}
                  rows={4}
                  disabled={!session}
                />
              </label>
              <div className="button-row">
                <button type="button" className="secondary-button" onClick={handleLoadChannelExample} disabled={!session || loading}>
                  Cargar conversacion ejemplo
                </button>
                <button type="submit" disabled={!session || loading || !channelText.trim()}>
                  Enviar al canal
                </button>
              </div>
            </form>

            <ListeningPipeline session={session} />
          </div>

          <ChannelChat session={session} />
          <StructuredPreview session={session} />
        </div>
      </section>

      <section className="main-grid">
        <section className="surface work-panel">
          <div className="section-heading">
            <span className="step-badge">1</span>
            <div>
              <h2>Entrada de coordinacion</h2>
              <p>Primero crea una sesion, luego envia texto libre para que el LLM lo convierta en datos.</p>
            </div>
          </div>

          <form className="stack" onSubmit={handleCreateSession}>
            <label>
              Nombre de la reunion
              <input value={title} onChange={(event) => setTitle(event.target.value)} />
            </label>
            <button type="submit" disabled={loading}>
              Crear sesion
            </button>
          </form>

          <form className="stack" onSubmit={handleSendMessage}>
            <label>
              Mensaje del grupo
              <textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                rows={7}
                disabled={!session}
              />
            </label>
            <div className="button-row">
              <button type="button" className="secondary-button" onClick={() => setMessage(EXAMPLE)}>
                Usar ejemplo
              </button>
              <button type="submit" disabled={!session || loading}>
                Extraer con LLM
              </button>
            </div>
          </form>

          <form className="manual-box" onSubmit={handleAddManual}>
            <h3>Correccion manual rapida</h3>
            <div className="manual-grid">
              <input
                value={manualName}
                onChange={(event) => setManualName(event.target.value)}
                placeholder="Participante"
                disabled={!session}
              />
              <select value={manualDay} onChange={(event) => setManualDay(event.target.value as Day)} disabled={!session}>
                {DAYS.map((day) => (
                  <option key={day} value={day}>
                    {day}
                  </option>
                ))}
              </select>
              <input value={manualStart} onChange={(event) => setManualStart(event.target.value)} disabled={!session} />
              <input value={manualEnd} onChange={(event) => setManualEnd(event.target.value)} disabled={!session} />
            </div>
            <button type="submit" disabled={!session || loading || !manualName.trim()}>
              Agregar disponibilidad
            </button>
          </form>

          <div className="button-row final-actions">
            <button type="button" disabled={!session || loading} onClick={handleCalculate}>
              Calcular mejores horarios
            </button>
            {llmSource ? (
              <span className="source-pill">
                LLM: {llmSource}
                {lastElapsedMs !== null ? ` - ${formatElapsed(lastElapsedMs)}` : ""}
                {lastTokenUsage ? ` - ${formatTokenUsage(lastTokenUsage)}` : ""}
              </span>
            ) : null}
          </div>
        </section>

        <section className="surface">
          <div className="section-heading">
            <span className="step-badge">2</span>
            <div>
              <h2>Lo que el sistema entendio</h2>
              <p>Esta vista hace visible el valor del LLM: texto informal convertido en datos revisables.</p>
            </div>
          </div>

          {session?.missing_info.length ? (
            <div className="warning-box">
              <strong>Datos faltantes</strong>
              {session.missing_info.map((item) => (
                <p key={item}>{item}</p>
              ))}
            </div>
          ) : null}

          {session?.participants.length ? (
            <div className="participant-list">
              {session.participants.map((participant) => (
                <article className="participant-card" key={participant.id}>
                  <div className="participant-title">
                    <h3>{participant.name}</h3>
                    <span>{participant.availability.length} horario(s)</span>
                  </div>
                  {participant.availability.length ? (
                    <div className="slot-list">
                      {participant.availability.map((slot) => (
                        <span className="slot-pill" key={`${slot.day}-${slot.start}-${slot.end}`}>
                          {slot.day} {slot.start}-{slot.end}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <p className="muted">Sin horario claro.</p>
                  )}
                </article>
              ))}
            </div>
          ) : (
            <EmptyState text="Crea una sesion y envia un mensaje para ver participantes y horarios." />
          )}

          {session?.insights.length ? (
            <div className="insight-box">
              <h3>Lectura del sistema</h3>
              {session.insights.map((insight) => (
                <p key={insight}>{insight}</p>
              ))}
            </div>
          ) : null}

          {session?.messages.length ? (
            <div className="timeline-box">
              <h3>Historial</h3>
              {session.messages.slice(-5).map((item) => (
                <div className={`timeline-item ${item.role}`} key={item.id}>
                  <span className="timeline-meta">{item.role}{item.source ? ` - ${item.source}` : ""}</span>
                  <span>{item.role}{item.source ? ` · ${item.source}` : ""}</span>
                  <p>{item.content}</p>
                  {item.token_usage ? <small>{formatTokenUsage(item.token_usage)}</small> : null}
                </div>
              ))}
            </div>
          ) : null}
        </section>
      </section>

      <section className="surface heatmap-section">
        <div className="section-heading">
          <span className="step-badge">3</span>
          <div>
            <h2>Mapa de disponibilidad</h2>
            <p>Mientras mas intenso el bloque, mas participantes pueden asistir en ese horario.</p>
          </div>
        </div>
        {session?.availability_matrix.length ? (
          <AvailabilityHeatmap matrix={session.availability_matrix} totalParticipants={session.participants.length} />
        ) : (
          <EmptyState text="El mapa aparece despues de extraer o agregar disponibilidades." />
        )}
      </section>

      <section className="surface options-section">
        <div className="section-heading">
          <span className="step-badge">4</span>
          <div>
            <h2>Opciones sugeridas y explicadas</h2>
            <p>El motor Python calcula la decision. El LLM ya no participa en esta parte.</p>
          </div>
        </div>

        {session?.options.length ? (
          <div className="option-grid">
            {session.options.map((option, index) => (
              <article className={index === 0 ? "option-card recommended" : "option-card"} key={option.id}>
                <div className="option-topline">
                  <span>{index === 0 ? "Recomendada" : `Opcion ${index + 1}`}</span>
                  <strong>{option.coverage_percent}%</strong>
                </div>
                <h3>
                  {option.day} {option.start}-{option.end}
                </h3>
                <p>{option.explanation}</p>
                <div className="split-list">
                  <div>
                    <span className="label">Asisten</span>
                    <p>{option.available_participants.join(", ") || "Nadie"}</p>
                  </div>
                  <div>
                    <span className="label">No calzan</span>
                    <p>{option.unavailable_participants.join(", ") || "Nadie"}</p>
                  </div>
                </div>
                <button disabled={loading || session.status === "confirmed"} onClick={() => handleConfirm(option.id)}>
                  Confirmar esta opcion
                </button>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState text="Cuando calcules, aqui apareceran las mejores opciones con cobertura y explicacion." />
        )}

        {session?.decision_summary ? <div className="summary-box">{session.decision_summary}</div> : null}
      </section>
    </main>
  );
}

function LogoMark() {
  return (
    <div className="logo-mark" aria-hidden="true">
      <span className="logo-node node-a" />
      <span className="logo-node node-b" />
      <span className="logo-node node-c" />
    </div>
  );
}

function ChannelChat({ session }: { session: Session | null }) {
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
            Crea una sesion y carga mensajes. La ultima frase del ejemplo invoca al agente con @coordina.
          </div>
        )}
      </div>
    </div>
  );
}

function ListeningPipeline({ session }: { session: Session | null }) {
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

function StructuredPreview({ session }: { session: Session | null }) {
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

function AvailabilityHeatmap({
  matrix,
  totalParticipants
}: {
  matrix: AvailabilityCell[];
  totalParticipants: number;
}) {
  const byKey = new Map(matrix.map((cell) => [`${cell.day}-${cell.start}`, cell]));

  return (
    <div className="heatmap-wrap">
      <div className="heatmap-grid">
        <div className="heatmap-corner">Hora</div>
        {DAYS.map((day) => (
          <div className="heatmap-day" key={day}>
            {day}
          </div>
        ))}
        {HOURS.map((hour) => (
          <FragmentRow key={hour} hour={hour} byKey={byKey} totalParticipants={totalParticipants} />
        ))}
      </div>
      <div className="heatmap-legend">
        <span className="legend-cell low" /> baja
        <span className="legend-cell mid" /> media
        <span className="legend-cell high" /> alta
      </div>
    </div>
  );
}

function FragmentRow({
  hour,
  byKey,
  totalParticipants
}: {
  hour: string;
  byKey: Map<string, AvailabilityCell>;
  totalParticipants: number;
}) {
  return (
    <>
      <div className="heatmap-hour">{hour}</div>
      {DAYS.map((day) => {
        const cell = byKey.get(`${day}-${hour}`);
        const level = getHeatLevel(cell?.coverage_percent ?? 0);
        return (
          <div className={`heatmap-cell ${level}`} key={`${day}-${hour}`} title={cell?.available_participants.join(", ")}>
            <strong>{cell?.score ?? 0}/{totalParticipants || 0}</strong>
            <span>{cell?.coverage_percent ?? 0}%</span>
          </div>
        );
      })}
    </>
  );
}

function Metric({ title, value, detail }: { title: string; value: string | number; detail: string }) {
  return (
    <article className="metric-card">
      <span>{title}</span>
      <strong>{value}</strong>
      <p>{detail}</p>
    </article>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state">{text}</div>;
}

function formatStatus(status: Session["status"] | undefined) {
  if (!status) return "sin sesion";
  if (status === "draft") return "borrador";
  if (status === "calculated") return "calculada";
  return "confirmada";
}

function formatElapsed(ms: number) {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function formatTokenUsage(usage: TokenUsage) {
  if (usage.cached) return "cache: 0 tokens";
  return `${usage.total_tokens} tokens, costo aprox. $${usage.estimated_cost_usd.toFixed(6)}`;
}

function formatTokenTotals(totals: TokenTotals) {
  return `${totals.totalTokens} tokens, $${totals.estimatedCostUsd.toFixed(6)}`;
}

type TokenTotals = {
  totalTokens: number;
  estimatedCostUsd: number;
};

function calculateTokenTotals(session: Session | null): TokenTotals {
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

function getHeatLevel(percent: number) {
  if (percent >= 75) return "high";
  if (percent >= 40) return "mid";
  if (percent > 0) return "low";
  return "zero";
}

function buildStructuredPreview(session: Session | null) {
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

function getErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Ocurrio un error inesperado";
}
