import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  addAvailability,
  archiveSession,
  cancelDecision,
  clearAuth,
  configureChannel,
  confirmOption,
  configureParticipantRequirements,
  createLlmKey,
  createSession,
  deleteLlmKey,
  exportCalendar,
  exportSession,
  exportSessionCsv,
  getSavedAuth,
  getOpsStatus,
  getRuntime,
  listLlmKeys,
  listSessions,
  removeParticipant,
  reopenSession,
  saveAuth,
  sendChannelBatch,
  testLlmKey,
  updateLlmKey,
  updateReplyFormat,
  validateLogin,
  ApiError,
  type AuthCredentials
} from "./api";
import { CHANNEL_EXAMPLE } from "./constants";
import { GeminiKeyPanel } from "./components/GeminiKeyPanel";
import { getErrorMessage } from "./format";
import type { Day, LlmKeyListResponse, OpsStatus, ReplyFormat, RuntimeInfo, Session } from "./types";

const BOT_PHONE_DISPLAY = import.meta.env.VITE_BOT_PHONE_NUMBER ?? "+56 9 3527 1985";
const BOT_PHONE_DIGITS = BOT_PHONE_DISPLAY.replace(/\D/g, "");
const PUBLIC_APP_URL = import.meta.env.VITE_API_URL ?? window.location.origin;
const AUTO_REFRESH_MS = 20000;

const REPLY_FORMATS: Array<{ value: ReplyFormat; label: string; hint: string }> = [
  { value: "both", label: "Texto + imagen", hint: "Respuesta completa" },
  { value: "text", label: "Solo texto", hint: "Mensaje liviano" },
  { value: "image", label: "Solo imagen", hint: "Calendario visual" }
];
const WEEKDAYS: Day[] = ["lunes", "martes", "miercoles", "jueves", "viernes"];

type AuthState = "checking" | "anonymous" | "authenticated";
type SessionFilter = "all" | "review" | "ready" | "confirmed" | "archived" | "groups" | "manual";
type SessionQuality = "archived" | "confirmed" | "ready" | "review" | "empty";

export function App() {
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [loginUser, setLoginUser] = useState("coordina");
  const [loginPassword, setLoginPassword] = useState("");
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [opsStatus, setOpsStatus] = useState<OpsStatus | null>(null);
  const [llmKeyState, setLlmKeyState] = useState<LlmKeyListResponse | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionFilter, setSessionFilter] = useState<SessionFilter>("all");
  const [sessionSearch, setSessionSearch] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastSyncedAt, setLastSyncedAt] = useState("");
  const [groupName, setGroupName] = useState("Grupo prueba TAVI");
  const [adminName, setAdminName] = useState("Nicolas");
  const [adminPhone, setAdminPhone] = useState("");
  const [inviteLink, setInviteLink] = useState("");
  const [copied, setCopied] = useState(false);
  const [replyCopied, setReplyCopied] = useState(false);
  const [reportCopied, setReportCopied] = useState(false);
  const [csvDownloaded, setCsvDownloaded] = useState(false);
  const [calendarDownloaded, setCalendarDownloaded] = useState(false);
  const [manualName, setManualName] = useState("");
  const [manualDay, setManualDay] = useState<Day>("lunes");
  const [manualStart, setManualStart] = useState("09:00");
  const [manualEnd, setManualEnd] = useState("10:00");
  const [configListening, setConfigListening] = useState(true);
  const [configStartHour, setConfigStartHour] = useState(9);
  const [configEndHour, setConfigEndHour] = useState(18);
  const [busyLabel, setBusyLabel] = useState("");
  const [error, setError] = useState("");
  const busyRef = useRef(false);
  const sessionsRequestSequence = useRef(0);

  const isBusy = Boolean(busyLabel);
  const adminDigits = adminPhone.replace(/\D/g, "");
  const openSessionCount = sessions.filter((item) => !item.archived_at).length;
  const realGroupCount = sessions.filter((item) => !item.archived_at && item.channel_config.group_jid).length;
  const manualSessionCount = openSessionCount - realGroupCount;
  const reviewSessionCount = sessions.filter((item) => {
    const tone = getSessionQuality(item).tone;
    return tone === "review" || tone === "empty";
  }).length;
  const readySessionCount = sessions.filter((item) => getSessionQuality(item).tone === "ready").length;
  const confirmedSessionCount = sessions.filter((item) => getSessionQuality(item).tone === "confirmed").length;
  const archivedSessionCount = sessions.filter((item) => item.archived_at).length;
  const currentChannelMessages = useMemo(() => (session ? channelMessagesForCurrentContext(session) : []), [session]);
  const latestMessageCount = currentChannelMessages.length;
  const gateway = opsStatus?.gateway ?? null;
  const botMetricValue = gateway?.connected
    ? "Vinculado"
    : gateway?.state === "logged_out"
      ? "Desvinculado"
      : gateway
        ? "Sin conexion"
        : "Revisando";
  const botMetricDetail = gateway?.connected
    ? gateway.dead_letter_messages || gateway.queue_overflow_count
      ? `Cola requiere revision: ${gateway.dead_letter_messages ?? 0} aislado(s)`
      : `${BOT_PHONE_DISPLAY} - ${gateway.known_groups ?? 0} grupo(s), ${gateway.pending_messages ?? 0} pendiente(s)`
    : gateway?.state === "reconnecting"
      ? `Reconectando (intento ${gateway.reconnect_attempt ?? 0})`
      : gateway?.state === "logged_out"
        ? "Requiere volver a vincular WhatsApp"
        : gateway
          ? `Estado: ${gateway.state}`
          : BOT_PHONE_DISPLAY;
  const bestOption = session?.options[0] ?? null;
  const confirmedOption = session?.selected_option ?? null;
  const lastProcessing = session?.last_processing ?? null;
  const confirmationIssues = session ? getConfirmationBlockers(session) : [];
  const currentDecisionHistory = session ? decisionHistoryForCurrentContext(session) : [];
  const latestDecision =
    currentDecisionHistory.length > 0
      ? currentDecisionHistory[currentDecisionHistory.length - 1]
      : null;
  const selectedHistory = useMemo(() => currentChannelMessages.slice(-14), [currentChannelMessages]);
  const visibleSessions = useMemo(
    () => filterSessions(sessions, sessionFilter, sessionSearch),
    [sessions, sessionFilter, sessionSearch]
  );
  const inviteMessage = useMemo(
    () => buildInviteMessage({ groupName, adminName, inviteLink }),
    [groupName, adminName, inviteLink]
  );
  const adminWhatsAppUrl = adminDigits
    ? `https://wa.me/${adminDigits}?text=${encodeURIComponent(inviteMessage)}`
    : "";
  const botDirectUrl = `https://wa.me/${BOT_PHONE_DIGITS}?text=${encodeURIComponent(
    "Hola, te voy a agregar a un grupo de prueba para coordinar horarios con @coordina."
  )}`;

  useEffect(() => {
    const savedAuth = getSavedAuth();
    if (!savedAuth) {
      setAuthState("anonymous");
      return;
    }

    setLoginUser(savedAuth.username);
    validateLogin(savedAuth)
      .then((info) => {
        setRuntime(info);
        setAuthState("authenticated");
        Promise.all([refreshSessions(undefined, { quiet: true }), refreshOpsStatus(), refreshLlmKeys()]).catch((err) =>
          setError(getErrorMessage(err))
        );
      })
      .catch(() => {
        clearAuth();
        setAuthState("anonymous");
      });
  }, []);

  useEffect(() => {
    if (authState !== "authenticated" || !autoRefresh) return;
    const intervalId = window.setInterval(() => {
      if (document.visibilityState !== "visible" || busyRef.current) return;
      Promise.all([refreshSessions(session?.id, { quiet: true }), refreshOpsStatus(), refreshLlmKeys()])
        .then(() => setError(""))
        .catch((err) => setError(getErrorMessage(err)));
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(intervalId);
  }, [authState, autoRefresh, session?.id]);

  useEffect(() => {
    if (!session) return;
    setConfigListening(session.channel_config.listening_enabled);
    setConfigStartHour(session.channel_config.workday_start_hour);
    setConfigEndHour(session.channel_config.workday_end_hour);
    setManualStart(`${String(session.channel_config.workday_start_hour).padStart(2, "0")}:00`);
    setManualEnd(`${String(Math.min(session.channel_config.workday_start_hour + 1, session.channel_config.workday_end_hour)).padStart(2, "0")}:00`);
  }, [session?.id, session?.channel_config.workday_start_hour, session?.channel_config.workday_end_hour, session?.channel_config.listening_enabled]);

  async function runAction(label: string, action: () => Promise<void>) {
    busyRef.current = true;
    sessionsRequestSequence.current += 1;
    setBusyLabel(label);
    setError("");
    setCopied(false);
    setReplyCopied(false);
    setReportCopied(false);
    setCsvDownloaded(false);
    setCalendarDownloaded(false);
    try {
      await action();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      busyRef.current = false;
      setBusyLabel("");
    }
  }

  async function refreshSessions(focusSessionId?: string, options: { quiet?: boolean } = {}) {
    const requestSequence = ++sessionsRequestSequence.current;
    if (!options.quiet) setSessionsLoading(true);
    try {
      const response = await listSessions();
      if (requestSequence !== sessionsRequestSequence.current) return;
      setSessions(response.sessions);
      const currentId = focusSessionId ?? session?.id ?? "";
      const selected = response.sessions.find((item) => item.id === currentId) ?? response.sessions[0] ?? null;
      setSession(selected);
      setLastSyncedAt(new Date().toISOString());
    } finally {
      if (!options.quiet) setSessionsLoading(false);
    }
  }

  async function refreshOpsStatus() {
    const response = await getOpsStatus();
    setOpsStatus(response);
  }

  async function refreshLlmKeys() {
    const response = await listLlmKeys();
    setLlmKeyState(response);
  }

  async function handleLogin(event: FormEvent) {
    event.preventDefault();
    const credentials: AuthCredentials = {
      username: loginUser.trim(),
      password: loginPassword
    };

    await runAction("Validando acceso...", async () => {
      const info = await validateLogin(credentials);
      saveAuth(credentials);
      setRuntime(info);
      setAuthState("authenticated");
      await Promise.all([refreshSessions(undefined, { quiet: true }), refreshOpsStatus(), refreshLlmKeys()]);
    });
  }

  function handleLogout() {
    clearAuth();
    setRuntime(null);
    setOpsStatus(null);
    setLlmKeyState(null);
    setSession(null);
    setSessions([]);
    setLoginPassword("");
    setAuthState("anonymous");
  }

  async function handleRefreshAll() {
    await runAction("Actualizando panel...", async () => {
      const [info] = await Promise.all([getRuntime(), refreshSessions(), refreshOpsStatus(), refreshLlmKeys()]);
      setRuntime(info);
    });
  }

  async function handleCreateLlmKey(name: string, secret: string) {
    let created = false;
    await runAction("Cifrando y guardando llave Gemini...", async () => {
      await createLlmKey(name, secret);
      const [info] = await Promise.all([getRuntime(), refreshLlmKeys()]);
      setRuntime(info);
      created = true;
    });
    return created;
  }

  async function handleUpdateLlmKey(credentialId: string, updates: { name?: string; enabled?: boolean }) {
    await runAction("Actualizando llave Gemini...", async () => {
      await updateLlmKey(credentialId, updates);
      const [info] = await Promise.all([getRuntime(), refreshLlmKeys()]);
      setRuntime(info);
    });
  }

  async function handleTestLlmKey(credentialId: string) {
    await runAction("Validando llave con Gemini...", async () => {
      const result = await testLlmKey(credentialId);
      await refreshLlmKeys();
      if (!result.ok) {
        throw new Error("Gemini no aceptó la llave. Revisa su estado en el panel.");
      }
      setRuntime(await getRuntime());
    });
  }

  async function handleDeleteLlmKey(credentialId: string, confirmName: string) {
    let deleted = false;
    await runAction("Eliminando llave Gemini...", async () => {
      await deleteLlmKey(credentialId, confirmName);
      const [info] = await Promise.all([getRuntime(), refreshLlmKeys()]);
      setRuntime(info);
      deleted = true;
    });
    return deleted;
  }

  async function handleRunDemo() {
    await runAction("Ejecutando prueba del canal...", async () => {
      const demoTitle = `Prueba WhatsApp - ${groupName.trim() || "Grupo prueba TAVI"}`;
      const reusableSession = sessions.find((item) => !item.channel_config.group_jid && item.title === demoTitle);
      const sessionId = reusableSession?.id ?? (await createSession(demoTitle)).session.id;
      const response = await sendChannelBatch(sessionId, CHANNEL_EXAMPLE);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleChangeReplyFormat(format: ReplyFormat) {
    if (!session || session.archived_at || session.channel_config.reply_format === format) return;
    await runAction("Actualizando formato...", async () => {
      const response = await updateReplyFormat(session, format);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleConfirmBest() {
    if (!session || !bestOption) return;
    await handleConfirmOption(bestOption.id);
  }

  async function handleConfirmOption(optionId: string) {
    if (!session || confirmationIssues.length) return;
    await runAction("Confirmando decision...", async () => {
      try {
        const response = await confirmOption(session.id, optionId, session.proposal_revision);
        setSession(response.session);
        await refreshSessions(response.session.id);
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          await refreshSessions(session.id);
          throw new Error(`${err.message} Actualice las opciones; revisalas y confirma nuevamente.`);
        }
        throw err;
      }
    });
  }

  async function handleCancelDecision() {
    if (!session?.selected_option) return;
    await runAction("Cancelando decision...", async () => {
      const response = await cancelDecision(session.id);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleToggleArchive() {
    if (!session) return;
    const archived = Boolean(session.archived_at);
    await runAction(archived ? "Reabriendo sesion..." : "Archivando sesion...", async () => {
      const response = archived ? await reopenSession(session.id) : await archiveSession(session.id);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleSaveChannelConfig(event: FormEvent) {
    event.preventDefault();
    if (!session || session.archived_at) return;
    if (configStartHour >= configEndHour) {
      setError("La hora de inicio debe ser anterior a la hora de fin.");
      return;
    }
    await runAction("Guardando configuracion...", async () => {
      const response = await configureChannel(session.id, {
        listening_enabled: configListening,
        workday_start_hour: configStartHour,
        workday_end_hour: configEndHour
      });
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleExportReport() {
    if (!session) return;
    await runAction("Generando reporte...", async () => {
      const report = await exportSession(session.id);
      await navigator.clipboard.writeText(report.text);
      downloadText(report.filename, report.text);
      setReportCopied(true);
    });
  }

  async function handleExportCsv() {
    if (!session) return;
    await runAction("Generando CSV...", async () => {
      const report = await exportSessionCsv(session.id);
      downloadText(report.filename, report.text, "text/csv;charset=utf-8");
      setCsvDownloaded(true);
    });
  }

  async function handleExportCalendar() {
    if (!session?.selected_option) return;
    await runAction("Generando calendario...", async () => {
      const event = await exportCalendar(session.id);
      downloadText(event.filename, event.text, "text/calendar;charset=utf-8");
      setCalendarDownloaded(true);
    });
  }

  async function handleRemoveParticipant(name: string) {
    if (!session || session.archived_at) return;
    await runAction(`Quitando ${name}...`, async () => {
      const response = await removeParticipant(session.id, name);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleSetParticipantRequirements(
    name: string,
    changes: { required?: boolean; priority?: number }
  ) {
    if (!session || session.archived_at) return;
    await runAction("Guardando requerimientos...", async () => {
      const response = await configureParticipantRequirements(session.id, name, changes);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleManualAvailability(event: FormEvent) {
    event.preventDefault();
    if (!session || session.archived_at) return;

    const name = manualName.trim();
    if (!name) {
      setError("Escribe el nombre de la persona que quieres corregir.");
      return;
    }
    if (manualStart >= manualEnd) {
      setError("La hora de inicio debe ser anterior a la hora de termino.");
      return;
    }

    await runAction("Guardando correccion...", async () => {
      const response = await addAvailability(session.id, name, manualDay, manualStart, manualEnd);
      setSession(response.session);
      setManualName("");
      await refreshSessions(response.session.id);
    });
  }

  async function handleCopyInvite() {
    await navigator.clipboard.writeText(inviteMessage);
    setCopied(true);
  }

  async function handleCopyReply() {
    if (!session?.last_agent_reply) return;
    await navigator.clipboard.writeText(session.last_agent_reply);
    setReplyCopied(true);
  }

  if (authState === "checking") {
    return (
      <main className="auth-page">
        <section className="auth-card narrow">
          <div className="brand-mark">C</div>
          <h1>Verificando acceso</h1>
          <p>Conectando con la API protegida.</p>
        </section>
      </main>
    );
  }

  if (authState === "anonymous") {
    return (
      <main className="auth-page">
        <section className="auth-card">
          <div className="auth-intro">
            <div className="brand-line">
              <div className="brand-mark">C</div>
              <span>Coordina WhatsApp</span>
            </div>
            <h1>Panel administrador</h1>
            <p>Operacion privada del bot, grupos, sesiones e historial.</p>
          </div>

          <form className="login-form" onSubmit={handleLogin}>
            <label>
              Usuario
              <input autoComplete="username" value={loginUser} onChange={(event) => setLoginUser(event.target.value)} />
            </label>
            <label>
              Contrasena
              <input
                autoComplete="current-password"
                type="password"
                value={loginPassword}
                onChange={(event) => setLoginPassword(event.target.value)}
              />
            </label>
            <button type="submit" disabled={isBusy || !loginUser.trim() || !loginPassword}>
              Entrar
            </button>
            {error ? <p className="form-error">{error}</p> : null}
            {busyLabel ? <p className="form-status">{busyLabel}</p> : null}
          </form>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-line">
          <div className="brand-mark">C</div>
          <div>
            <strong>Coordina WhatsApp</strong>
            <span>{session ? sessionDisplayName(session) : "Panel operativo"}</span>
          </div>
        </div>
        <div className="topbar-actions">
          <span className="sync-label">{lastSyncedAt ? `Actualizado ${formatShortTime(lastSyncedAt)}` : "Sin sincronizar"}</span>
          <label className="toggle-pill">
            <input checked={autoRefresh} type="checkbox" onChange={(event) => setAutoRefresh(event.target.checked)} />
            Auto
          </label>
          <a className="ghost-button" href={PUBLIC_APP_URL} target="_blank" rel="noreferrer">
            Abrir sitio
          </a>
          <button className="ghost-button" type="button" onClick={handleRefreshAll} disabled={isBusy}>
            Actualizar
          </button>
          <button className="danger-button" type="button" onClick={handleLogout}>
            Salir
          </button>
        </div>
      </header>

      {error ? <div className="notice error">{error}</div> : null}
      {busyLabel ? <div className="notice active">{busyLabel}</div> : null}

      <section className="metrics-row" aria-label="Estado del sistema">
        <Metric
          label="Bot"
          value={botMetricValue}
          detail={botMetricDetail}
          tone={
            gateway?.connected && !gateway.dead_letter_messages && !gateway.queue_overflow_count
              ? "ok"
              : gateway
                ? "warn"
                : "neutral"
          }
        />
        <Metric label="Abiertas" value={String(openSessionCount)} detail={`${archivedSessionCount} archivadas`} tone="ok" />
        <Metric label="Revision" value={String(reviewSessionCount)} detail={`${readySessionCount} listas`} tone={reviewSessionCount ? "warn" : "ok"} />
        <Metric label="Mensajes" value={String(latestMessageCount)} detail="sesion activa" tone="neutral" />
        <Metric
          label="Backend"
          value={runtime?.provider_label ?? "Revisando"}
          detail={lastProcessing ? `${lastProcessing.confidence_label} - ${lastProcessing.source}` : runtime?.gemini_active_key_name ? `Activa: ${runtime.gemini_active_key_name}` : "Sin Gemini"}
          tone={runtime?.warnings.length || (lastProcessing && lastProcessing.confidence !== "high") ? "warn" : "ok"}
        />
        <Metric label="Seguridad" value="HTTPS" detail="API protegida" tone="ok" />
      </section>

      <section className="workspace">
        <aside className="panel sessions-panel">
          <div className="panel-head">
            <div>
              <span>Sesiones</span>
              <strong>{visibleSessions.length}</strong>
            </div>
            <button className="icon-button" type="button" onClick={() => refreshSessions()} disabled={isBusy || sessionsLoading}>
              Sync
            </button>
          </div>

          <label className="search-box">
            Buscar
            <input
              placeholder="Grupo, mensaje, participante..."
              value={sessionSearch}
              onChange={(event) => setSessionSearch(event.target.value)}
            />
          </label>

          <div className="filter-row" role="tablist" aria-label="Filtrar sesiones">
            <FilterButton active={sessionFilter === "all"} onClick={() => setSessionFilter("all")}>
              Abiertas <span>{openSessionCount}</span>
            </FilterButton>
            <FilterButton active={sessionFilter === "review"} onClick={() => setSessionFilter("review")}>
              Revisar <span>{reviewSessionCount}</span>
            </FilterButton>
            <FilterButton active={sessionFilter === "ready"} onClick={() => setSessionFilter("ready")}>
              Listas <span>{readySessionCount}</span>
            </FilterButton>
            <FilterButton active={sessionFilter === "confirmed"} onClick={() => setSessionFilter("confirmed")}>
              Confirmadas <span>{confirmedSessionCount}</span>
            </FilterButton>
            <FilterButton active={sessionFilter === "archived"} onClick={() => setSessionFilter("archived")}>
              Archivadas <span>{archivedSessionCount}</span>
            </FilterButton>
            <FilterButton active={sessionFilter === "groups"} onClick={() => setSessionFilter("groups")}>
              Grupos <span>{realGroupCount}</span>
            </FilterButton>
            <FilterButton active={sessionFilter === "manual"} onClick={() => setSessionFilter("manual")}>
              Pruebas <span>{manualSessionCount}</span>
            </FilterButton>
          </div>

          <div className="session-list" aria-label="Sesiones disponibles">
            {sessionsLoading ? <div className="empty-state">Cargando sesiones...</div> : null}
            {!sessionsLoading && !sessions.length ? <div className="empty-state">Sin sesiones registradas.</div> : null}
            {!sessionsLoading && sessions.length > 0 && !visibleSessions.length ? (
              <div className="empty-state">No hay resultados para el filtro actual.</div>
            ) : null}
            {visibleSessions.map((item) => (
              <SessionButton item={item} key={item.id} selected={session?.id === item.id} onSelect={() => setSession(item)} />
            ))}
          </div>
        </aside>

        <section className="panel detail-panel">
          {session ? (
            <>
              <div className="detail-head">
                <div>
                  <span>{session.channel_config.group_jid ? "Grupo WhatsApp" : "Sesion manual"}</span>
                  <h2>{sessionDisplayName(session)}</h2>
                  <p>{session.channel_config.group_jid ?? "Sesion creada desde panel o API"}</p>
                </div>
                <div className="detail-actions">
                  <div className={`status-badge ${session.status}`}>{getSessionQuality(session).label}</div>
                  <button className="ghost-button compact" type="button" onClick={handleToggleArchive} disabled={isBusy}>
                    {session.archived_at ? "Reabrir" : "Archivar"}
                  </button>
                </div>
              </div>

              <div className="facts-grid">
                <Fact label="Calidad" value={getSessionQuality(session).label} />
                <Fact label="Integrantes actuales" value={String(session.channel_config.group_participant_count ?? "N/D")} />
                <Fact label="Con disponibilidad" value={String(session.participants.length)} />
                <Fact label="Revision vigente" value={`R${session.proposal_revision}`} />
                <Fact label="Mensajes" value={String(latestMessageCount)} />
                {session.habitual_slot ? (
                  <Fact
                    label="Hora de siempre"
                    value={`${session.habitual_slot.day} ${session.habitual_slot.start}-${session.habitual_slot.end}`}
                  />
                ) : null}
              </div>

              <div className="decision-grid">
                {bestOption ? (
                  <>
                    <div className="decision-card primary">
                      <span>{confirmedOption ? "Decision confirmada" : "Mejor opcion"}</span>
                      <strong>
                        {(confirmedOption ?? bestOption).day} {(confirmedOption ?? bestOption).start}-{(confirmedOption ?? bestOption).end}
                      </strong>
                      <p>{(confirmedOption ?? bestOption).coverage_percent}% de cobertura</p>
                      <div className="card-actions">
                        <button
                          type="button"
                          onClick={handleConfirmBest}
                          disabled={isBusy || Boolean(confirmedOption) || confirmationIssues.length > 0}
                        >
                          Confirmar
                        </button>
                        <button className="ghost-button compact" type="button" onClick={handleExportCalendar} disabled={isBusy || !confirmedOption}>
                          {calendarDownloaded ? "Calendario listo" : "Calendario"}
                        </button>
                        <button className="ghost-button compact" type="button" onClick={handleExportReport} disabled={isBusy}>
                          {reportCopied ? "Exportado" : "Exportar"}
                        </button>
                        <button className="ghost-button compact" type="button" onClick={handleExportCsv} disabled={isBusy}>
                          {csvDownloaded ? "CSV listo" : "CSV"}
                        </button>
                        <button className="danger-button compact" type="button" onClick={handleCancelDecision} disabled={isBusy || !confirmedOption || Boolean(session.archived_at)}>
                          Cancelar
                        </button>
                      </div>
                    </div>
                    {confirmationIssues.length && !confirmedOption ? (
                      <div className="empty-state wide">
                        <strong>Confirmacion protegida</strong>
                        <p>{confirmationIssues.join(" ")}</p>
                      </div>
                    ) : null}
                    <div className="decision-card">
                      <span>Asisten</span>
                      <p>{(confirmedOption ?? bestOption).available_participants.join(", ") || "Sin participantes claros"}</p>
                      {(confirmedOption ?? bestOption).unavailable_participants.length ? (
                        <>
                          <span>No calzan</span>
                          <p>{(confirmedOption ?? bestOption).unavailable_participants.join(", ")}</p>
                        </>
                      ) : null}
                    </div>
                  </>
                ) : (
                  <div className="empty-state wide">Aun no hay opcion calculada para esta sesion.</div>
                )}
              </div>

              {session.options.length ? (
                <div className="options-block">
                  <div className="section-title">
                    <span>Alternativas</span>
                    <small>{session.options.length} calculadas</small>
                  </div>
                  <div className="option-list">
                    {session.options.map((option, index) => {
                      const selected = confirmedOption?.id === option.id;
                      return (
                        <article className={selected ? "option-row selected" : "option-row"} key={option.id}>
                          <div>
                            <strong>
                              {index + 1}. {option.day} {option.start}-{option.end}
                            </strong>
                            <p>
                              {option.coverage_percent}% - {option.available_participants.join(", ") || "sin asistentes"}
                            </p>
                          </div>
                          <button
                            className={selected ? "ghost-button compact" : "compact"}
                            type="button"
                            onClick={() => handleConfirmOption(option.id)}
                            disabled={isBusy || selected || confirmationIssues.length > 0}
                          >
                            {selected ? "Confirmada" : "Confirmar"}
                          </button>
                        </article>
                      );
                    })}
                  </div>
                </div>
              ) : null}

              <div className="people-block">
                <div className="section-title">
                  <span>Personas</span>
                  <small>{session.participants.length} detectadas</small>
                </div>
                <form className="correction-form" onSubmit={handleManualAvailability}>
                  <label>
                    Nombre
                    <input
                      placeholder="Ej. Luisa"
                      value={manualName}
                      onChange={(event) => setManualName(event.target.value)}
                    />
                  </label>
                  <label>
                    Dia
                    <select value={manualDay} onChange={(event) => setManualDay(event.target.value as Day)}>
                      {WEEKDAYS.map((day) => (
                        <option key={day} value={day}>
                          {day}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Inicio
                    <input type="time" step="3600" value={manualStart} onChange={(event) => setManualStart(event.target.value)} />
                  </label>
                  <label>
                    Fin
                    <input type="time" step="3600" value={manualEnd} onChange={(event) => setManualEnd(event.target.value)} />
                  </label>
                  <button type="submit" disabled={isBusy || !manualName.trim() || Boolean(session.archived_at)}>
                    Agregar horario
                  </button>
                </form>
                {session.participants.length ? (
                  <div className="people-list">
                    {session.participants.map((participant) => (
                      <article className="person-row" key={participant.id}>
                        <div>
                          <strong>
                            {participant.name}
                            {participant.required ? " ⭐" : ""}
                            {participant.priority ? ` (pri ${participant.priority})` : ""}
                          </strong>
                          <p>{formatSlots(participant.availability)}</p>
                          <div className="person-controls">
                            <label className="inline-label">
                              <input
                                type="checkbox"
                                checked={Boolean(participant.required)}
                                disabled={isBusy || Boolean(session.archived_at)}
                                onChange={(event) =>
                                  handleSetParticipantRequirements(participant.name, {
                                    required: event.target.checked
                                  })
                                }
                              />
                              Requerido
                            </label>
                            <label className="inline-label">
                              Prioridad
                              <select
                                value={participant.priority ?? 0}
                                disabled={isBusy || Boolean(session.archived_at)}
                                onChange={(event) =>
                                  handleSetParticipantRequirements(participant.name, {
                                    priority: Number(event.target.value)
                                  })
                                }
                              >
                                {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((value) => (
                                  <option key={value} value={value}>
                                    {value || "—"}
                                  </option>
                                ))}
                              </select>
                            </label>
                          </div>
                        </div>
                        <button
                          className="ghost-button compact"
                          type="button"
                          onClick={() => handleRemoveParticipant(participant.name)}
                          disabled={isBusy || Boolean(session.archived_at)}
                        >
                          Quitar
                        </button>
                      </article>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state">Sin personas detectadas.</div>
                )}
              </div>

              <div className="ops-grid">
                <div className={`ops-card ${lastProcessing?.confidence ?? "neutral"}`}>
                  <span>Procesamiento</span>
                  <strong>{lastProcessing?.confidence_label ?? "Sin datos"}</strong>
                  <p>{lastProcessing ? `${lastProcessing.source} - ${lastProcessing.detail}` : "Aun no hay extraccion registrada."}</p>
                  {lastProcessing?.retrieval_used ? (
                    <p className="ops-card-meta">
                      RAG: {lastProcessing.retrieval_participant_count ?? 0} en roster
                      {typeof lastProcessing.retrieval_past_decision_count === "number"
                        ? `, ${lastProcessing.retrieval_past_decision_count} decisiones previas`
                        : ""}
                      {lastProcessing.retrieval_preview ? ` — ${lastProcessing.retrieval_preview}` : ""}
                    </p>
                  ) : null}
                  {lastProcessing?.habitual_used ? (
                    <p className="ops-card-meta">Resolvio "a la hora de siempre" con el horario habitual.</p>
                  ) : null}
                </div>
                <div className="ops-card">
                  <span>Decision</span>
                  <strong>{latestDecision ? `${latestDecision.option.day} ${latestDecision.option.start}` : "Pendiente"}</strong>
                  <p>{latestDecision ? `${latestDecision.confirmed_by} - ${formatTimestamp(latestDecision.created_at)}` : "Todavia no hay horario confirmado."}</p>
                </div>
                <div className="ops-card">
                  <span>Faltantes</span>
                  <strong>{session.missing_info.length}</strong>
                  <p>{session.missing_info[0] ?? "Sin pendientes criticos."}</p>
                </div>
              </div>

              <div className="history-block">
                <div className="section-title">
                  <span>Historial</span>
                  <small>Ultimos {selectedHistory.length} mensajes</small>
                </div>
                {selectedHistory.length ? (
                  <div className="message-list">
                    {selectedHistory.map((message) => (
                      <article className={`message-item ${message.kind}`} key={message.id}>
                        <span>
                          {message.sender} - {formatTimestamp(message.created_at)}
                          {message.detected_invocation ? " - invocacion" : ""}
                        </span>
                        <p>{message.text}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state">Sin mensajes del canal.</div>
                )}
              </div>

              {session.last_agent_reply ? (
                <div className="reply-block">
                <div className="section-title">
                  <span>Ultima respuesta</span>
                  <div className="button-row">
                    <button className="ghost-button compact" type="button" onClick={handleExportReport} disabled={isBusy}>
                      {reportCopied ? "Exportado" : "Exportar"}
                    </button>
                    <button className="ghost-button compact" type="button" onClick={handleExportCsv} disabled={isBusy}>
                      {csvDownloaded ? "CSV listo" : "CSV"}
                    </button>
                    <button className="ghost-button compact" type="button" onClick={handleCopyReply}>
                      {replyCopied ? "Copiada" : "Copiar"}
                    </button>
                  </div>
                </div>
                  <pre>{session.last_agent_reply}</pre>
                </div>
              ) : null}
            </>
          ) : (
            <div className="empty-state">Selecciona una sesion.</div>
          )}
        </section>

        <aside className="side-stack">
          <GeminiKeyPanel
            state={llmKeyState}
            busy={isBusy}
            onCreate={handleCreateLlmKey}
            onUpdate={handleUpdateLlmKey}
            onTest={handleTestLlmKey}
            onDelete={handleDeleteLlmKey}
          />

          <section className="panel">
            <div className="panel-head">
              <div>
                <span>Coordinacion</span>
                <strong>Ventana horaria</strong>
              </div>
            </div>
            {session ? (
              <form className="form-grid" onSubmit={handleSaveChannelConfig}>
                <label>
                  Desde
                  <select value={configStartHour} onChange={(event) => setConfigStartHour(Number(event.target.value))}>
                    {Array.from({ length: 23 }, (_, hour) => (
                      <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>
                    ))}
                  </select>
                </label>
                <label>
                  Hasta
                  <select value={configEndHour} onChange={(event) => setConfigEndHour(Number(event.target.value))}>
                    {Array.from({ length: 23 }, (_, index) => index + 1).map((hour) => (
                      <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>
                    ))}
                  </select>
                </label>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={configListening}
                    onChange={(event) => setConfigListening(event.target.checked)}
                  />
                  Escuchar mensajes nuevos
                </label>
                <button type="submit" disabled={isBusy || Boolean(session.archived_at) || configStartHour >= configEndHour}>
                  Guardar limites
                </button>
                <small>
                  El LLM interpreta el lenguaje; las propuestas se limitan a {String(configStartHour).padStart(2, "0")}:00-{String(configEndHour).padStart(2, "0")}:00.
                </small>
              </form>
            ) : (
              <div className="empty-state">Sin sesion seleccionada.</div>
            )}
          </section>

          <section className="panel">
            <div className="panel-head">
              <div>
                <span>Respuesta</span>
                <strong>{session ? replyFormatLabel(session.channel_config.reply_format) : "N/D"}</strong>
              </div>
            </div>
            {session ? (
              <div className="format-grid">
                {REPLY_FORMATS.map((option) => (
                  <button
                    className={session.channel_config.reply_format === option.value ? "format-button active" : "format-button"}
                    disabled={isBusy || Boolean(session.archived_at)}
                    key={option.value}
                    type="button"
                    onClick={() => handleChangeReplyFormat(option.value)}
                  >
                    <strong>{option.label}</strong>
                    <span>{option.hint}</span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="empty-state">Sin sesion seleccionada.</div>
            )}
          </section>

          <section className="panel">
            <div className="panel-head">
              <div>
                <span>Comandos WA</span>
                <strong>@coordina</strong>
              </div>
            </div>
            <div className="command-list">
              <code>@coordina</code>
              <span>actualiza opciones con mensajes nuevos</span>
              <code>@coordina confirmar 1 {session ? `R${session.proposal_revision}` : "R..."}</code>
              <span>cierra la alternativa de la revision mostrada</span>
              <code>@coordina faltan</code>
              <span>lista pendientes por persona</span>
              <code>@coordina exportar</code>
              <span>envia reporte textual</span>
              <code>@coordina quitar Ana</code>
              <span>corrige participantes mal detectados</span>
              <code>@coordina reinicia historial</code>
              <span>inicia otra coordinacion desde cero</span>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <div>
                <span>Invitacion</span>
                <strong>Agregar bot</strong>
              </div>
              <a className="ghost-button compact" href={botDirectUrl} target="_blank" rel="noreferrer">
                Chat bot
              </a>
            </div>

            <div className="bot-number">
              <span>Numero del bot</span>
              <strong>{BOT_PHONE_DISPLAY}</strong>
            </div>

            <div className="form-grid">
              <label>
                Grupo
                <input value={groupName} onChange={(event) => setGroupName(event.target.value)} />
              </label>
              <label>
                Admin
                <input value={adminName} onChange={(event) => setAdminName(event.target.value)} />
              </label>
              <label>
                WhatsApp admin
                <input inputMode="tel" placeholder="+569..." value={adminPhone} onChange={(event) => setAdminPhone(event.target.value)} />
              </label>
              <label>
                Link grupo
                <input placeholder="https://chat.whatsapp.com/..." value={inviteLink} onChange={(event) => setInviteLink(event.target.value)} />
              </label>
            </div>

            <div className="invite-text">{inviteMessage}</div>

            <div className="button-row">
              <button type="button" onClick={handleCopyInvite}>
                {copied ? "Copiado" : "Copiar"}
              </button>
              <a className={adminWhatsAppUrl ? "primary-link" : "primary-link disabled"} href={adminWhatsAppUrl || undefined} target="_blank" rel="noreferrer">
                Enviar
              </a>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <div>
                <span>Prueba</span>
                <strong>Canal simulado</strong>
              </div>
            </div>
            <div className="mini-thread">
              {CHANNEL_EXAMPLE.map((message) => (
                <div className="mini-message" key={`${message.sender}-${message.text}`}>
                  <span>{message.sender}</span>
                  <p>{message.text}</p>
                </div>
              ))}
            </div>
            <button type="button" onClick={handleRunDemo} disabled={isBusy}>
              Ejecutar prueba
            </button>
          </section>
        </aside>
      </section>
    </main>
  );
}

function buildInviteMessage({
  groupName,
  adminName,
  inviteLink
}: {
  groupName: string;
  adminName: string;
  inviteLink: string;
}) {
  const safeGroup = groupName.trim() || "el grupo de prueba";
  const safeAdmin = adminName.trim() || "admin";
  const linkLine = inviteLink.trim() ? ` Link del grupo: ${inviteLink.trim()}.` : "";

  return `Hola ${safeAdmin}, agrega el bot ${BOT_PHONE_DISPLAY} al grupo "${safeGroup}".${linkLine} Luego escriban disponibilidades e invoquen con "@coordina".`;
}

function filterSessions(sessions: Session[], filter: SessionFilter, search: string) {
  const query = normalizeSearch(search);
  return sessions.filter((item) => {
    const quality = getSessionQuality(item).tone;
    if (filter === "archived") {
      if (!item.archived_at) return false;
    } else if (item.archived_at) {
      return false;
    }

    if (filter === "review" && quality !== "review" && quality !== "empty") return false;
    if (filter === "ready" && quality !== "ready") return false;
    if (filter === "confirmed" && quality !== "confirmed") return false;
    if (filter === "groups" && !item.channel_config.group_jid) return false;
    if (filter === "manual" && item.channel_config.group_jid) return false;
    if (!query) return true;

    const searchable = normalizeSearch(
      [
        sessionDisplayName(item),
        item.title,
        item.channel_config.group_jid ?? "",
        item.channel_config.group_name ?? "",
        item.participants.map((participant) => participant.name).join(" "),
        lastHumanMessage(item) ?? "",
        item.last_agent_reply ?? ""
      ].join(" ")
    );
    return searchable.includes(query);
  });
}

function getSessionQuality(session: Session): { tone: SessionQuality; label: string; detail: string } {
  if (session.archived_at) {
    return { tone: "archived", label: "Archivada", detail: `Archivada ${formatShortDate(session.archived_at)}` };
  }

  if (session.status === "confirmed" && session.selected_option) {
    return { tone: "confirmed", label: "Confirmada", detail: "Horario cerrado" };
  }

  if (session.missing_info.length > 0) {
    return { tone: "review", label: "Revisar", detail: `${session.missing_info.length} pendiente(s)` };
  }

  const blockers = getConfirmationBlockers(session);
  if (blockers.length > 0) {
    return { tone: "review", label: "Revisar", detail: blockers[0] };
  }

  if (session.options.length > 0) {
    return { tone: "ready", label: "Lista", detail: "Puede confirmarse" };
  }

  if (session.participants.length > 0) {
    return { tone: "review", label: "Incompleta", detail: "Sin opcion calculable" };
  }

  return { tone: "empty", label: "Sin datos", detail: "Aun sin disponibilidad" };
}

function getConfirmationBlockers(session: Session): string[] {
  const blockers: string[] = [];
  if (session.archived_at) blockers.push("La sesion esta archivada.");
  if (session.missing_info.length > 0) blockers.push("Falta informacion del grupo antes de confirmar.");
  if (session.last_processing && (session.last_processing.confidence === "low" || session.last_processing.fallback_used)) {
    blockers.push("La ultima interpretacion debe revisarse o repetirse con el LLM principal.");
  }
  return blockers;
}

function normalizeSearch(value: string) {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "");
}

function sessionDisplayName(session: Session) {
  return session.channel_config.group_name || session.title || "Sesion sin nombre";
}

function lastHumanMessage(session: Session) {
  const message = [...channelMessagesForCurrentContext(session)].reverse().find((item) => item.kind === "human");
  if (!message) return null;
  return `${message.sender}: ${message.text}`;
}

function channelMessagesForCurrentContext(session: Session) {
  if (!session.channel_context_message_id) return session.channel_messages;
  const contextIndex = session.channel_messages.findIndex((item) => item.id === session.channel_context_message_id);
  return contextIndex >= 0 ? session.channel_messages.slice(contextIndex) : session.channel_messages;
}

function decisionHistoryForCurrentContext(session: Session) {
  if (!session.channel_context_message_id) return session.decision_history;
  const boundary = session.channel_messages.find((item) => item.id === session.channel_context_message_id)?.created_at;
  if (!boundary) return session.decision_history;
  const boundaryTime = new Date(boundary).getTime();
  if (Number.isNaN(boundaryTime)) return session.decision_history;
  return session.decision_history.filter((item) => new Date(item.created_at).getTime() >= boundaryTime);
}

function formatTimestamp(value: string) {
  try {
    return new Intl.DateTimeFormat("es-CL", {
      dateStyle: "short",
      timeStyle: "short"
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function formatShortTime(value: string) {
  try {
    return new Intl.DateTimeFormat("es-CL", {
      hour: "2-digit",
      minute: "2-digit"
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function formatShortDate(value: string) {
  try {
    return new Intl.DateTimeFormat("es-CL", {
      day: "2-digit",
      month: "2-digit"
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function formatSlots(slots: Session["participants"][number]["availability"]) {
  if (!slots.length) return "Sin disponibilidad clara";
  return slots.map((slot) => `${slot.day} ${slot.start}-${slot.end}`).join(", ");
}

function downloadText(filename: string, text: string, type = "text/plain;charset=utf-8") {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function replyFormatLabel(format: ReplyFormat) {
  if (format === "image") return "Solo imagen";
  if (format === "text") return "Solo texto";
  return "Texto + imagen";
}

function Metric({
  label,
  value,
  detail,
  tone
}: {
  label: string;
  value: string;
  detail: string;
  tone: "ok" | "warn" | "neutral";
}) {
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <p>{detail}</p>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function SessionButton({
  item,
  selected,
  onSelect
}: {
  item: Session;
  selected: boolean;
  onSelect: () => void;
}) {
  const quality = getSessionQuality(item);
  return (
    <button className={selected ? "session-row selected" : "session-row"} type="button" onClick={onSelect}>
      <span className="session-title-line">
        <span className="session-title">{sessionDisplayName(item)}</span>
        <span className={`quality-chip ${quality.tone}`}>{quality.label}</span>
      </span>
      <span className="session-meta">
        {item.channel_config.group_jid ? "Grupo" : "Prueba"} - {channelMessagesForCurrentContext(item).length} mensajes - {quality.detail}
      </span>
      <span className="session-last">{lastHumanMessage(item) ?? "Sin historial del canal"}</span>
    </button>
  );
}

function FilterButton({
  active,
  onClick,
  children
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button className={active ? "filter-tab active" : "filter-tab"} type="button" role="tab" aria-selected={active} onClick={onClick}>
      {children}
    </button>
  );
}
