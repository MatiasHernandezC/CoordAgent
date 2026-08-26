import { FormEvent, useEffect, useMemo, useState } from "react";

import type { AppUser, Session } from "../types";

type UserUpdates = { display_name?: string; active?: boolean };

type Props = {
  users: AppUser[];
  sessions: Session[];
  currentUser: AppUser;
  selectedSession: Session | null;
  busy: boolean;
  onUpdateUser: (username: string, updates: UserUpdates) => Promise<void>;
  onResetPassword: (username: string, password: string) => Promise<void>;
  onToggleAccess: (username: string) => Promise<void>;
  onTransferOwner: (username: string) => Promise<void>;
};

export function UserManagementPanel({
  users,
  sessions,
  currentUser,
  selectedSession,
  busy,
  onUpdateUser,
  onResetPassword,
  onToggleAccess,
  onTransferOwner
}: Props) {
  const [editingUsername, setEditingUsername] = useState("");
  const [displayNameDraft, setDisplayNameDraft] = useState("");
  const [passwordUsername, setPasswordUsername] = useState("");
  const [passwordDraft, setPasswordDraft] = useState("");
  const [ownerDraft, setOwnerDraft] = useState("");

  const groupAdmins = useMemo(
    () => users.filter((user) => !user.is_admin),
    [users]
  );
  const activeOwnerCandidates = groupAdmins.filter(
    (user) => user.active && user.username !== selectedSession?.owner_username
  );

  useEffect(() => {
    setOwnerDraft("");
  }, [selectedSession?.id, selectedSession?.owner_username]);

  function startEditing(user: AppUser) {
    setEditingUsername(user.username);
    setDisplayNameDraft(user.display_name);
    setPasswordUsername("");
    setPasswordDraft("");
  }

  async function saveDisplayName(event: FormEvent, user: AppUser) {
    event.preventDefault();
    const displayName = displayNameDraft.trim();
    if (displayName.length < 2 || displayName === user.display_name) return;
    await onUpdateUser(user.username, { display_name: displayName });
    setEditingUsername("");
  }

  async function savePassword(event: FormEvent, username: string) {
    event.preventDefault();
    if (passwordDraft.length < 10) return;
    await onResetPassword(username, passwordDraft);
    setPasswordUsername("");
    setPasswordDraft("");
  }

  async function toggleActive(user: AppUser) {
    const action = user.active ? "desactivar" : "reactivar";
    const warning = user.active
      ? "La cuenta perderá acceso inmediatamente. Sus grupos e historial se conservarán."
      : "La cuenta recuperará el acceso con su contraseña actual.";
    if (!window.confirm(`¿Quieres ${action} a ${user.display_name}?\n\n${warning}`)) return;
    await onUpdateUser(user.username, { active: !user.active });
  }

  async function transferOwner() {
    if (!selectedSession || !ownerDraft) return;
    const target = users.find((user) => user.username === ownerDraft);
    if (!target) return;
    if (!window.confirm(
      `¿Transferir "${selectedSession.title}" a ${target.display_name}?\n\nEl propietario anterior perderá el acceso salvo que se lo asignes nuevamente.`
    )) return;
    await onTransferOwner(ownerDraft);
    setOwnerDraft("");
  }

  return <>
    <div className="user-management-list">
      {users.map((user) => {
        const ownedGroups = sessions.filter((item) => item.owner_username === user.username).length;
        const assignedGroups = sessions.filter((item) => item.assigned_usernames.includes(user.username)).length;
        const isSelf = user.username === currentUser.username;
        return <article className={`managed-user ${user.active ? "" : "inactive"}`} key={user.username}>
          <div className="managed-user-head">
            <div>
              <strong>{user.display_name}</strong>
              <small>@{user.username}</small>
            </div>
            <div className="user-badges">
              <span className={`status-badge ${user.active ? "ready" : "archived"}`}>
                {user.active ? "Activo" : "Inactivo"}
              </span>
              <span className="role-badge">{user.is_admin ? "Plataforma" : "Grupos"}</span>
            </div>
          </div>
          <div className="user-group-counts">
            <span><strong>{ownedGroups}</strong> propios</span>
            <span><strong>{assignedGroups}</strong> asignados</span>
            <span>{user.created_at ? `Creado ${new Date(user.created_at).toLocaleDateString("es-CL")}` : "Cuenta existente"}</span>
          </div>
          <div className="managed-user-actions">
            <button className="ghost-button compact" type="button" disabled={busy} onClick={() => startEditing(user)}>
              Editar nombre
            </button>
            <button
              className="ghost-button compact"
              type="button"
              disabled={busy}
              onClick={() => {
                setPasswordUsername(user.username);
                setPasswordDraft("");
                setEditingUsername("");
              }}
            >
              Nueva contraseña
            </button>
            <button
              className={`${user.active ? "danger-button" : "ghost-button"} compact`}
              type="button"
              disabled={busy || isSelf}
              title={isSelf ? "No puedes desactivar tu propia cuenta" : ""}
              onClick={() => toggleActive(user)}
            >
              {user.active ? "Desactivar" : "Reactivar"}
            </button>
          </div>
          {editingUsername === user.username ? <form className="inline-user-form" onSubmit={(event) => saveDisplayName(event, user)}>
            <label>Nombre visible<input autoFocus value={displayNameDraft} maxLength={80} onChange={(event) => setDisplayNameDraft(event.target.value)} /></label>
            <button type="submit" disabled={busy || displayNameDraft.trim().length < 2 || displayNameDraft.trim() === user.display_name}>Guardar</button>
            <button className="ghost-button" type="button" onClick={() => setEditingUsername("")}>Cancelar</button>
          </form> : null}
          {passwordUsername === user.username ? <form className="inline-user-form" onSubmit={(event) => savePassword(event, user.username)}>
            <label>Nueva contraseña<input autoFocus type="password" minLength={10} maxLength={128} autoComplete="new-password" value={passwordDraft} onChange={(event) => setPasswordDraft(event.target.value)} /></label>
            <button type="submit" disabled={busy || passwordDraft.length < 10}>Restablecer</button>
            <button className="ghost-button" type="button" onClick={() => setPasswordUsername("")}>Cancelar</button>
          </form> : null}
        </article>;
      })}
    </div>

    <div className="access-list user-access-section">
      <span className="access-list-title">Acceso a {selectedSession ? selectedSession.title : "un grupo"}</span>
      {groupAdmins.map((user) => {
        const assigned = Boolean(selectedSession?.assigned_usernames.includes(user.username));
        return <label className={`access-user ${user.active ? "" : "inactive"}`} key={user.username}>
        <input
          type="checkbox"
          checked={assigned}
          disabled={busy || !selectedSession || (!user.active && !assigned)}
          onChange={() => onToggleAccess(user.username)}
        />
        <span><strong>{user.display_name}</strong><small>@{user.username}{user.active ? "" : " · cuenta inactiva"}</small></span>
      </label>})}
      {!groupAdmins.length ? <div className="empty-state">Crea el primer usuario para asignarle este grupo.</div> : null}
    </div>

    <div className="owner-transfer">
      <div>
        <span className="access-list-title">Propietario del grupo seleccionado</span>
        <strong>{selectedSession?.owner_username ? `@${selectedSession.owner_username}` : "Sin propietario"}</strong>
        <small>Transfiere la propiedad antes de desactivar al responsable actual.</small>
      </div>
      <select value={ownerDraft} disabled={busy || !selectedSession} onChange={(event) => setOwnerDraft(event.target.value)}>
        <option value="">Selecciona nuevo propietario</option>
        {activeOwnerCandidates.map((user) => <option key={user.username} value={user.username}>{user.display_name} (@{user.username})</option>)}
      </select>
      <button type="button" disabled={busy || !selectedSession || !ownerDraft} onClick={transferOwner}>Transferir</button>
    </div>
  </>;
}
