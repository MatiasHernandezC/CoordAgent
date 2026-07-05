import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  addAvailability,
  calculateOptions,
  configureChannel,
  confirmOption,
  createSession,
  getRuntime,
  sendChannelBatch,
  sendChannelMessage,
  sendMessage
} from "./api";
import { CHANNEL_EXAMPLE, DAYS, EXAMPLE } from "./constants";
import {
  calculateTokenTotals,
  formatElapsed,
  formatStatus,
  formatTokenTotals,
  formatTokenUsage,
  getErrorMessage
} from "./format";
import { AvailabilityHeatmap } from "./components/AvailabilityHeatmap";
import { ChannelChat, ListeningPipeline, StructuredPreview } from "./components/channel";
import { EmptyState, LogoMark, Metric } from "./components/ui";
import type { Day, RuntimeInfo, Session, TokenUsage } from "./types";

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
  const [inputMode, setInputMode] = useState<"form" | "channel">("form");
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
      // El backend crea el participante si no existe, asi que basta una sola llamada.
      const availabilityResponse = await addAvailability(
        session.id,
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
              <strong>Coord-Agent</strong>
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

      <section className="surface step-block">
        <div className="section-heading">
          <span className="step-badge">1</span>
          <div>
            <h2>Crear sesion</h2>
            <p>Empieza aqui: nombra la reunion para habilitar los pasos siguientes.</p>
          </div>
        </div>
        <form className="session-form" onSubmit={handleCreateSession}>
          <label>
            Nombre de la reunion
            <input value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <button type="submit" disabled={loading}>
            {session ? "Crear otra sesion" : "Crear sesion"}
          </button>
        </form>
        {session ? (
          <div className="session-chip">
            <span className="session-dot" aria-hidden="true" />
            Sesion activa: <strong>{session.title}</strong>
          </div>
        ) : (
          <p className="hint-text">Aun no hay sesion activa. Crea una para comenzar.</p>
        )}
      </section>

      <section className="surface step-block">
        <div className="section-heading">
          <span className="step-badge">2</span>
          <div>
            <h2>Ingresar disponibilidad</h2>
            <p>Captura los horarios de dos formas. Elige una pestana; puedes combinarlas.</p>
          </div>
        </div>

        <div className="tab-bar" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "form"}
            className={inputMode === "form" ? "tab-btn active" : "tab-btn"}
            onClick={() => setInputMode("form")}
          >
            Mensaje directo
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "channel"}
            className={inputMode === "channel" ? "tab-btn active" : "tab-btn"}
            onClick={() => setInputMode("channel")}
          >
            Canal simulado (WhatsApp)
          </button>
        </div>

        {!session ? (
          <EmptyState text="Crea una sesion en el paso 1 para habilitar la entrada de disponibilidad." />
        ) : inputMode === "form" ? (
          <div className="mode-panel">
            <form className="stack" onSubmit={handleSendMessage}>
              <label>
                Mensaje del grupo
                <textarea
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  rows={6}
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
          </div>
        ) : (
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
        )}

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

      <section className="surface step-block">
        <div className="section-heading">
          <span className="step-badge">3</span>
          <div>
            <h2>Lo que el sistema entendio</h2>
            <p>El texto informal convertido en datos revisables y corregibles.</p>
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
                <p>{item.content}</p>
                {item.token_usage ? <small>{formatTokenUsage(item.token_usage)}</small> : null}
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <section className="surface step-block">
        <div className="section-heading">
          <span className="step-badge">4</span>
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

      <section className="surface step-block">
        <div className="section-heading">
          <span className="step-badge">5</span>
          <div>
            <h2>Opciones y decision</h2>
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
