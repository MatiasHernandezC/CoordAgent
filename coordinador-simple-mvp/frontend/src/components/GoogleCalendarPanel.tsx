import type { GoogleCalendarStatus } from "../types";

type Props = {
  state: GoogleCalendarStatus | null;
  busy: boolean;
  onConnect: () => Promise<void>;
  onDisconnect: () => Promise<void>;
};

export function GoogleCalendarPanel({ state, busy, onConnect, onDisconnect }: Props) {
  return (
    <section className="panel key-panel">
      <div className="panel-head">
        <div>
          <span>Google Calendar</span>
          <strong>{state?.connected ? "Cuenta conectada" : "Sin conectar"}</strong>
        </div>
        <span className={`status-dot ${state?.connected ? "ok" : "warn"}`} aria-hidden="true" />
      </div>

      {!state?.configured ? (
        <div className="inline-warning">
          Define <code>GOOGLE_CLIENT_ID</code>, <code>GOOGLE_CLIENT_SECRET</code> y{" "}
          <code>GOOGLE_OAUTH_REDIRECT_URI</code> para habilitar la creacion real de eventos.
        </div>
      ) : state.connected ? (
        <div className="key-item">
          <div className="key-title">
            <div>
              <strong>{state.account_email ?? "Cuenta de Google"}</strong>
              <span>Conectada {state.connected_at ? formatDate(state.connected_at) : ""}</span>
            </div>
          </div>
          <small>
            Al confirmar una opcion, ademas del link y el .ics se crea el evento real en esta cuenta.
          </small>
          <div className="key-actions">
            <button className="danger-button compact" type="button" disabled={busy} onClick={onDisconnect}>
              Desconectar
            </button>
          </div>
        </div>
      ) : (
        <div className="key-create-form">
          <small>
            Conecta una cuenta de Google para que las confirmaciones creen el evento directamente en el
            calendario, ademas del link y el archivo .ics que ya se envian.
          </small>
          <button type="button" disabled={busy} onClick={onConnect}>
            Conectar Google Calendar
          </button>
        </div>
      )}
    </section>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("es-CL", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}
