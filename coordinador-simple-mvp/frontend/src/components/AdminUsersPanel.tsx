import type { AdminUser } from "../types";

type Props = {
  users: AdminUser[] | null;
  busy: boolean;
  onSetSuperadmin: (actor: string, isSuperadmin: boolean) => Promise<void>;
};

export function AdminUsersPanel({ users, busy, onSetSuperadmin }: Props) {
  const superadminCount = users?.filter((user) => user.is_superadmin).length ?? 0;

  return (
    <section className="panel key-panel">
      <div className="panel-head">
        <div>
          <span>Administradores</span>
          <strong>{users ? `${users.length} conocidos, ${superadminCount} superadmin` : "Revisando"}</strong>
        </div>
        <span className={`status-dot ${superadminCount > 0 ? "ok" : "warn"}`} aria-hidden="true" />
      </div>

      <small>
        Se registra cada admin apenas entra al panel. Los logins (usuario/clave) siguen fijos en el
        despliegue; esto solo controla quien es superadmin.
      </small>

      {!users || users.length === 0 ? (
        <div className="empty-state compact">Todavia no hay admins registrados.</div>
      ) : (
        users.map((user) => (
          <div className="key-item" key={user.actor}>
            <div className="key-title">
              <div>
                <strong>{user.actor}</strong>
                <span>
                  {user.owned_groups} grupo{user.owned_groups === 1 ? "" : "s"} a cargo
                  {user.last_seen_at ? ` · visto ${formatDate(user.last_seen_at)}` : ""}
                </span>
              </div>
            </div>
            <div className="key-actions">
              {user.superadmin_locked ? (
                <small>Superadmin fijo (SUPERADMIN_USERS)</small>
              ) : user.is_superadmin ? (
                <button
                  className="danger-button compact"
                  type="button"
                  disabled={busy}
                  onClick={() => onSetSuperadmin(user.actor, false)}
                >
                  Quitar superadmin
                </button>
              ) : (
                <button
                  className="compact"
                  type="button"
                  disabled={busy}
                  onClick={() => onSetSuperadmin(user.actor, true)}
                >
                  Hacer superadmin
                </button>
              )}
            </div>
          </div>
        ))
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
