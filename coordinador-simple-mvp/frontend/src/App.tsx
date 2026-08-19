import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  addAvailability,
  assignSessionChief,
  assignSessionUsers,
  archiveSession,
  cancelDecision,
  clearAuth,
  configureChannel,
  confirmOption,
  configureParticipantRequirements,
  createUser,
  createLlmKey,
  createSession,
  deleteLlmKey,
  disconnectGoogleCalendar,
  exportCalendar,
  exportSession,
  exportSessionCsv,
  getSavedAuth,
  getGoogleCalendarAuthUrl,
  getGoogleCalendarStatus,
  getCurrentUser,
  getOpsStatus,
  getRuntime,
  listLlmKeys,
  listUsers,
  listSessions,
  removeParticipant,
  registerUser,
  reopenSession,
  saveAuth,
  sendChannelBatch,
  testLlmKey,
  updateLlmKey,
  updateReplyFormat,
  ApiError,
  type AuthCredentials
} from "./api";
import { CHANNEL_EXAMPLE } from "./constants";
import { GeminiKeyPanel } from "./components/GeminiKeyPanel";
import { GoogleCalendarPanel } from "./components/GoogleCalendarPanel";
import { getErrorMessage } from "./format";
import type { AppUser, Day, GoogleCalendarStatus, LlmKeyListResponse, OpsStatus, ReplyFormat, RuntimeInfo, Session } from "./types";

const BOT_PHONE_FALLBACK = "Numero no disponible hasta vincular WhatsApp";
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
  const [currentUser, setCurrentUser] = useState<AppUser | null>(null);
  const [users, setUsers] = useState<AppUser[]>([]);
  const [loginUser, setLoginUser] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [registerDisplayName, setRegisterDisplayName] = useState("");
  const [registerUsername, setRegisterUsername] = useState("");
  const [registerPassword, setRegisterPassword] = useState("");
  const [newOwnedGroupName, setNewOwnedGroupName] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [opsStatus, setOpsStatus] = useState<OpsStatus | null>(null);
  const [llmKeyState, setLlmKeyState] = useState<LlmKeyListResponse | null>(null);
  const [googleCalendarState, setGoogleCalendarState] = useState<GoogleCalendarStatus | null>(null);
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
  const isAdmin = currentUser?.is_admin === true;
  const ownsCurrentSession = Boolean(
    session && currentUser && session.owner_username === currentUser.username
  );
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
  const botPhoneDisplay = gateway?.bot_phone_number || BOT_PHONE_FALLBACK;
  const botPhoneDigits = (gateway?.bot_phone_number || "").replace(/\D/g, "");
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
      : `${botPhoneDisplay} - ${gateway.known_groups ?? 0} grupo(s), ${gateway.pending_messages ?? 0} pendiente(s)`
    : gateway?.state === "reconnecting"
      ? `Reconectando (intento ${gateway.reconnect_attempt ?? 0})`
      : gateway?.state === "logged_out"
        ? "Requiere volver a vincular WhatsApp"
        : gateway
          ? `Estado: ${gateway.state}`
          : botPhoneDisplay;
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
    () => buildInviteMessage({ groupName, adminName, inviteLink, botPhone: botPhoneDisplay }),
    [groupName, adminName, inviteLink, botPhoneDisplay]
  );
  const adminWhatsAppUrl = adminDigits
    ? `https://wa.me/${adminDigits}?text=${encodeURIComponent(inviteMessage)}`
    : "";
  const botDirectUrl = botPhoneDigits ? `https://wa.me/${botPhoneDigits}?text=${encodeURIComponent(
    "Hola, te voy a agregar a un grupo de prueba para coordinar horarios con @coordina."
  )}` : "";

  useEffect(() => {
    if (currentUser?.display_name || currentUser?.username) {
      setAdminName(currentUser.display_name || currentUser.username);
    }
  }, [currentUser]);

  useEffect(() => {
    const savedAuth = getSavedAuth();
    if (!savedAuth) {
      setAuthState("anonymous");
      return;
    }

    setLoginUser(savedAuth.username);
    getCurrentUser(savedAuth)
      .then(async ({ user }) => {
        setCurrentUser(user);
        setAuthState("authenticated");
        const adminRequests = user.is_admin
          ? [refreshRuntime(), refreshLlmKeys(), refreshGoogleCalendar(), refreshUsers()]
          : [];
        Promise.all([refreshSessions(undefined, { quiet: true }), refreshOpsStatus(), ...adminRequests]).catch((err) =>
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
      const adminRequests = isAdmin
        ? [refreshRuntime(), refreshLlmKeys(), refreshGoogleCalendar(), refreshUsers()]
        : [];
      Promise.all([refreshSessions(session?.id, { quiet: true }), refreshOpsStatus(), ...adminRequests])
        .then(() => setError(""))
        .catch((err) => setError(getErrorMessage(err)));
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(intervalId);
  }, [authState, autoRefresh, isAdmin, session?.id]);

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

  async function refreshRuntime() {
    setRuntime(await getRuntime());
  }

  async function refreshUsers() {
    const response = await listUsers();
    setUsers(response.users);
  }

  async function refreshLlmKeys() {
    const response = await listLlmKeys();
    setLlmKeyState(response);
  }

  async function refreshGoogleCalendar() {
    const response = await getGoogleCalendarStatus();
    setGoogleCalendarState(response);
  }

  async function handleLogin(event: FormEvent) {
    event.preventDefault();
    const credentials: AuthCredentials = {
      username: loginUser.trim(),
      password: loginPassword
    };

    await runAction("Validando acceso...", async () => {
      const { user } = await getCurrentUser(credentials);
      saveAuth(credentials);
      setCurrentUser(user);
      setAuthState("authenticated");
      const adminRequests = user.is_admin
        ? [refreshRuntime(), refreshLlmKeys(), refreshGoogleCalendar(), refreshUsers()]
        : [];
      await Promise.all([refreshSessions(undefined, { quiet: true }), refreshOpsStatus(), ...adminRequests]);
    });
  }

  async function handleRegister(event: FormEvent) {
    event.preventDefault();
    const credentials: AuthCredentials = {
      username: registerUsername.trim(),
      password: registerPassword
    };
    await runAction("Creando tu cuenta...", async () => {
      const { user } = await registerUser(
        credentials.username,
        registerDisplayName.trim(),
        credentials.password
      );
      saveAuth(credentials);
      setCurrentUser(user);
      setLoginUser(credentials.username);
      setRegisterPassword("");
      setAuthState("authenticated");
      await refreshSessions(undefined, { quiet: true });
    });
  }

  async function handleCreateOwnedGroup(event: FormEvent) {
    event.preventDefault();
    const title = newOwnedGroupName.trim();
    if (!title) return;
    await runAction("Creando tu grupo...", async () => {
      const response = await createSession(title);
      setNewOwnedGroupName("");
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  function handleLogout() {
    clearAuth();
    setCurrentUser(null);
    setUsers([]);
    setRuntime(null);
    setOpsStatus(null);
    setLlmKeyState(null);
    setGoogleCalendarState(null);
    setSession(null);
    setSessions([]);
    setLoginPassword("");
    setAuthState("anonymous");
  }

  async function handleRefreshAll() {
    await runAction("Actualizando panel...", async () => {
      const adminRequests = isAdmin
        ? [refreshRuntime(), refreshLlmKeys(), refreshGoogleCalendar(), refreshUsers()]
        : [];
      await Promise.all([refreshSessions(), refreshOpsStatus(), ...adminRequests]);
    });
  }

  async function handleCreateUser(event: FormEvent) {
    event.preventDefault();
    await runAction("Creando usuario...", async () => {
      await createUser(newUsername.trim(), newDisplayName.trim(), newPassword);
      setNewUsername("");
      setNewDisplayName("");
      setNewPassword("");
      await refreshUsers();
    });
  }

  async function handleToggleSessionUser(username: string) {
    if (!session) return;
    const assigned = session.assigned_usernames.includes(username);
    const next = assigned
      ? session.assigned_usernames.filter((value) => value !== username)
      : [...session.assigned_usernames, username];
    await runAction("Actualizando acceso al grupo...", async () => {
      const response = await assignSessionUsers(session.id, next);
      setSession(response.session);
      await refreshSessions(response.session.id);
    });
  }

  async function handleAssignChief(participantId: string) {
    if (!session) return;
    await runAction("Asignando jefe del grupo...", async () => {
      const response = await assignSessionChief(session.id, participantId);
      setSession(response.session);
      await refreshSessions(response.session.id);
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

  async function handleConnectGoogleCalendar() {
    await runAction("Abriendo autorizacion de Google...", async () => {
      const { auth_url } = await getGoogleCalendarAuthUrl();
      window.open(auth_url, "_blank", "noopener");
    });
  }

  async function handleDisconnectGoogleCalendar() {
    await runAction("Desconectando Google Calendar...", async () => {
      await disconnectGoogleCalendar();
      await refreshGoogleCalendar();
    });
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
    changes: { required?: boolean }
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
              <span>Coordina</span>
            </div>
            <h1>{authMode === "login" ? "Acceso al panel" : "Crea tu cuenta"}</h1>
            <p>
              {authMode === "login"
                ? "Administra tus propios grupos sin acceder a información de otras cuentas."
                : "Tu cuenta será administradora únicamente de los grupos que tú crees."}
            </p>
          </div>

          {authMode === "login" ? <form className="login-form" onSubmit={handleLogin}>
            <label>
              Usuario
              <input autoComplete="username" value={loginUser} onChange={(event) => setLoginUser(event.target.value)} />
            </label>
            <label>
              Contraseña
              <input
                autoComplete="current-password"
                type="password"
                value={loginPassword}
                onChange={(event) => setLoginPassword(event.target.value)}
              />
            </label>
            <button type="submit" disabled={isBusy || !loginUser.trim() || !loginPassword}>
              Ingresar
            </button>
            {error ? <p className="form-error">{error}</p> : null}
            {busyLabel ? <p className="form-status">{busyLabel}</p> : null}
            <button className="auth-switch" type="button" onClick={() => { setError(""); setAuthMode("register"); }}>
              Crear una cuenta
            </button>
          </form> : <form className="login-form" onSubmit={handleRegister}>
            <label>
              Nombre
              <input autoComplete="name" value={registerDisplayName} onChange={(event) => setRegisterDisplayName(event.target.value)} />
            </label>
            <label>
              Usuario
              <input autoComplete="username" value={registerUsername} onChange={(event) => setRegisterUsername(event.target.value)} />
            </label>
            <label>
              Contraseña
              <input autoComplete="new-password" minLength={10} type="password" value={registerPassword} onChange={(event) => setRegisterPassword(event.target.value)} />
            </label>
            <small>Mínimo 10 caracteres. Esta cuenta no tendrá permisos de plataforma.</small>
            <button type="submit" disabled={isBusy || registerDisplayName.trim().length < 2 || registerUsername.trim().length < 2 || registerPassword.length < 10}>
              Registrarme
            </button>
            {error ? <p className="form-error">{error}</p> : null}
            {busyLabel ? <p className="form-status">{busyLabel}</p> : null}
            <button className="auth-switch" type="button" onClick={() => { setError(""); setAuthMode("login"); }}>
              Ya tengo una cuenta
            </button>
          </form>}
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
            <strong>Coordina</strong>
            <span>{session ? `${channelLabel(session)} · ${sessionDisplayName(session)}` : "Panel operativo"}</span>
          </div>
        </div>
        <div className="topbar-actions">
          <span className="account-label">
            {currentUser?.is_admin ? "Administrador de plataforma" : `Administrador de grupos · ${currentUser?.display_name}`}
          </span>
          <span className="sync-label">{lastSyncedAt ? `Actualizado ${formatShortTime(lastSyncedAt)}` : "Sin sincronizar"}</span>
          {isAdmin ? <>
            <label className="toggle-pill">
              <input checked={autoRefresh} type="checkbox" onChange={(event) => setAutoRefresh(event.target.checked)} />
              Auto
            </label>
            <a className="ghost-button" href={PUBLIC_APP_URL} target="_blank" rel="noreferrer">
              Abrir sitio
            </a>
          </> : null}
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

      {isAdmin ? <section className="metrics-row" aria-label="Estado del sistema">
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
      </section> : null}

      <section className="workspace">
        <aside className="panel sessions-panel">
          <div className="panel-head">
            <div>
              <span>{isAdmin ? "Sesiones" : "Mis grupos"}</span>
              <strong>{visibleSessions.length}</strong>
            </div>
            <button className="icon-button" type="button" onClick={() => refreshSessions()} disabled={isBusy || sessionsLoading}>
              Sync
            </button>
          </div>

          {!isAdmin ? <form className="owned-group-form" onSubmit={handleCreateOwnedGroup}>
            <label>
              Crear grupo
              <input
                placeholder="Ej. Equipo de proyecto"
                value={newOwnedGroupName}
                onChange={(event) => setNewOwnedGroupName(event.target.value)}
              />
            </label>
            <button type="submit" disabled={isBusy || newOwnedGroupName.trim().length < 2}>
              Nuevo grupo
            </button>
          </form> : null}

          <label className="search-box">
            Buscar
            <input
              placeholder="Grupo, mensaje, participante..."
              value={sessionSearch}
              onChange={(event) => setSessionSearch(event.target.value)}
            />
          </label>

          {isAdmin ? <div className="filter-row" role="tablist" aria-label="Filtrar sesiones">
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
          </div> : <p className="normal-user-list-hint">Aquí aparecen únicamente los grupos que creaste o que te delegaron.</p>}

          <div className="session-list" aria-label="Sesiones disponibles">
            {sessionsLoading ? <div className="empty-state">Cargando sesiones...</div> : null}
            {!sessionsLoading && !sessions.length ? <div className="empty-state">{isAdmin ? "Sin sesiones registradas." : "Crea tu primer grupo para obtener el código de vinculación."}</div> : null}
            {!sessionsLoading && sessions.length > 0 && !visibleSessions.length ? (
              <div className="empty-state">No hay resultados para el filtro actual.</div>
            ) : null}
            {visibleSessions.map((item) => (
              <SessionButton item={item} key={item.id} selected={session?.id === item.id} onSelect={() => setSession(item)} />
            ))}
          </div>
        </aside>

        <section className="panel detail-panel">
          {session ? (isAdmin || ownsCurrentSession ? (
            <>
              <div className="detail-head">
                <div>
                  <span>{session.channel_config.group_jid ? `Grupo ${channelLabel(session)}` : "Sesion manual"}</span>
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
            <>
              <div className="detail-head">
                <div>
                  <span>Grupo {channelLabel(session)}</span>
                  <h2>{sessionDisplayName(session)}</h2>
                  <p>Información y coordinación del grupo asignado.</p>
                </div>
                <div className={`status-badge ${session.status}`}>{getSessionQuality(session).label}</div>
              </div>

              <div className="facts-grid normal-facts">
                <Fact label="Canal" value={channelLabel(session)} />
                <Fact label="Integrantes" value={String(session.channel_config.group_participant_count ?? session.participants.length)} />
                <Fact label="Jefe" value={chiefName(session) ?? "Sin asignar"} />
                <Fact label="Mensajes" value={String(selectedHistory.length)} />
              </div>

              <div className="people-block">
                <div className="section-title">
                  <span>Integrantes</span>
                  <small>Selecciona quién será jefe del grupo</small>
                </div>
                {session.participants.length ? <div className="people-list">
                  {session.participants.map((participant) => {
                    const selected = session.chief_participant_id === participant.id;
                    return <article className={selected ? "person-row chief" : "person-row"} key={participant.id}>
                      <div>
                        <strong>{participant.name}</strong>
                        <p>{formatSlots(participant.availability)}</p>
                      </div>
                      <button className={selected ? "ghost-button compact active-chief" : "ghost-button compact"} type="button" disabled={isBusy || selected} onClick={() => handleAssignChief(participant.id)}>
                        {selected ? "Jefe asignado" : "Asignar jefe"}
                      </button>
                    </article>;
                  })}
                </div> : <div className="empty-state">Aún no hay integrantes detectados.</div>}
              </div>

              <div className="options-block">
                <div className="section-title">
                  <span>Horarios propuestos</span>
                  <small>{session.options.length} opciones</small>
                </div>
                {session.options.length ? <div className="option-list">
                  {session.options.map((option, index) => <article className="option-row" key={option.id}>
                    <div>
                      <strong>{index + 1}. {option.day} {option.start}-{option.end}</strong>
                      <p>{option.coverage_percent}% · {option.available_participants.join(", ") || "sin asistentes"}</p>
                    </div>
                  </article>)}
                </div> : <div className="empty-state">Todavía no hay horarios calculados.</div>}
              </div>

              <div className="history-block">
                <div className="section-title">
                  <span>Mensajes recientes</span>
                  <small>Últimos {selectedHistory.length}</small>
                </div>
                {selectedHistory.length ? <div className="message-list">
                  {selectedHistory.map((message) => <article className={`message-item ${message.kind}`} key={message.id}>
                    <span>{message.sender} · {formatTimestamp(message.created_at)}</span>
                    <p>{message.text}</p>
                  </article>)}
                </div> : <div className="empty-state">No hay mensajes registrados.</div>}
              </div>
            </>
          )) : (
            <div className="empty-state">Selecciona una sesion.</div>
          )}
        </section>

        <aside className="side-stack">
          {!isAdmin && ownsCurrentSession && session ? <section className="panel link-group-panel">
            <div className="panel-head">
              <div>
                <span>WhatsApp</span>
                <strong>{session.channel_config.group_jid ? "Grupo vinculado" : "Vincular este grupo"}</strong>
              </div>
              <span className={session.channel_config.group_jid ? "status-dot online" : "status-dot pending"} />
            </div>
            <div className="bot-number">
              <span>Número del bot</span>
              <strong>{botPhoneDisplay}</strong>
            </div>
            {session.channel_config.group_jid ? <>
              <p className="link-success">Conectado con {session.channel_config.group_name || "tu grupo de WhatsApp"}.</p>
              <small>Los mensajes con @coordina llegarán solamente a este espacio.</small>
            </> : <>
              <ol className="link-steps">
                <li>Crea el grupo en WhatsApp.</li>
                <li>Agrega el número del bot.</li>
                <li>Envía este comando dentro del grupo:</li>
              </ol>
              <code className="link-command">@coordina vincular {session.link_code}</code>
              <button type="button" onClick={() => navigator.clipboard.writeText(`@coordina vincular ${session.link_code}`)}>
                Copiar comando
              </button>
              <small>El código funciona una sola vez y vincula únicamente este grupo.</small>
            </>}
          </section> : null}

          {isAdmin ? <section className="panel user-access-panel">
            <div className="panel-head">
              <div>
                <span>Accesos</span>
                <strong>Usuarios y grupos</strong>
              </div>
            </div>
            <form className="form-grid user-create-form" onSubmit={handleCreateUser}>
              <label>Usuario<input value={newUsername} placeholder="ej. daniel" onChange={(event) => setNewUsername(event.target.value)} /></label>
              <label>Nombre visible<input value={newDisplayName} placeholder="Daniel Eguíluz" onChange={(event) => setNewDisplayName(event.target.value)} /></label>
              <label>Contraseña<input type="password" minLength={10} autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label>
              <button type="submit" disabled={isBusy || newUsername.trim().length < 2 || newDisplayName.trim().length < 2 || newPassword.length < 10}>Crear usuario</button>
            </form>
            <div className="access-list">
              <span className="access-list-title">Acceso a {session ? sessionDisplayName(session) : "un grupo"}</span>
              {users.filter((user) => !user.is_admin).map((user) => <label className="access-user" key={user.username}>
                <input type="checkbox" checked={Boolean(session?.assigned_usernames.includes(user.username))} disabled={isBusy || !session} onChange={() => handleToggleSessionUser(user.username)} />
                <span><strong>{user.display_name}</strong><small>@{user.username}</small></span>
              </label>)}
              {!users.some((user) => !user.is_admin) ? <div className="empty-state">Crea el primer usuario para asignarle este grupo.</div> : null}
            </div>
          </section> : null}

          {isAdmin ? <>
          <GeminiKeyPanel
            state={llmKeyState}
            busy={isBusy}
            onCreate={handleCreateLlmKey}
            onUpdate={handleUpdateLlmKey}
            onTest={handleTestLlmKey}
            onDelete={handleDeleteLlmKey}
          />

          <GoogleCalendarPanel
            state={googleCalendarState}
            busy={isBusy}
            onConnect={handleConnectGoogleCalendar}
            onDisconnect={handleDisconnectGoogleCalendar}
          />
          </> : null}

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

          {isAdmin || ownsCurrentSession ? <>
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
              <strong>{botPhoneDisplay}</strong>
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
          </> : null}
        </aside>
      </section>
    </main>
  );
}

function buildInviteMessage({
  groupName,
  adminName,
  inviteLink,
  botPhone
}: {
  groupName: string;
  adminName: string;
  inviteLink: string;
  botPhone: string;
}) {
  const safeGroup = groupName.trim() || "el grupo de prueba";
  const safeAdmin = adminName.trim() || "admin";
  const linkLine = inviteLink.trim() ? ` Link del grupo: ${inviteLink.trim()}.` : "";

  return `Hola ${safeAdmin}, agrega el bot ${botPhone} al grupo "${safeGroup}".${linkLine} Luego escriban disponibilidades e invoquen con "@coordina".`;
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

function channelLabel(session: Session) {
  const groupJid = session.channel_config.group_jid?.toLowerCase() ?? "";
  if (!groupJid) return "Prueba";
  if (groupJid.startsWith("slack:")) return "Slack";
  if (groupJid.endsWith("@g.us")) return "WhatsApp";
  return "Canal";
}

function chiefName(session: Session) {
  return session.participants.find((participant) => participant.id === session.chief_participant_id)?.name ?? null;
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
        {channelLabel(item)} - {channelMessagesForCurrentContext(item).length} mensajes - {quality.detail}
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
