import { FormEvent, useState } from "react";

import type { LlmKey, LlmKeyListResponse } from "../types";

type KeyUpdates = { name?: string; enabled?: boolean };

type Props = {
  state: LlmKeyListResponse | null;
  busy: boolean;
  onCreate: (name: string, secret: string) => Promise<boolean>;
  onUpdate: (credentialId: string, updates: KeyUpdates) => Promise<void>;
  onTest: (credentialId: string) => Promise<void>;
  onDelete: (credentialId: string, confirmName: string) => Promise<boolean>;
};

export function GeminiKeyPanel({ state, busy, onCreate, onUpdate, onTest, onDelete }: Props) {
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    const created = await onCreate(name.trim(), secret);
    if (!created) return;
    setName("");
    setSecret("");
  }

  const deleteKey = state?.keys.find((key) => key.id === deleteId) ?? null;

  return (
    <section className="panel key-panel">
      <div className="panel-head">
        <div>
          <span>LLM Gemini</span>
          <strong>{state ? `${state.available_count}/${state.keys.length} llaves disponibles` : "Revisando llaves"}</strong>
        </div>
        <span className={`status-dot ${state?.available_count ? "ok" : "warn"}`} aria-hidden="true" />
      </div>

      {!state?.management_enabled ? (
        <div className="inline-warning">
          Define <code>LLM_KEYS_MASTER_KEY</code> en producción para habilitar el almacén cifrado.
        </div>
      ) : (
        <form className="key-create-form" onSubmit={handleCreate}>
          <label>
            Nombre visible
            <input
              maxLength={80}
              placeholder="Proyecto respaldo"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Llave Gemini
            <input
              autoComplete="new-password"
              maxLength={512}
              placeholder="Se cifra y no se vuelve a mostrar"
              type="password"
              value={secret}
              onChange={(event) => setSecret(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy || name.trim().length < 2 || secret.length < 8}>
            Guardar llave cifrada
          </button>
          <small>
            El orden de respaldo se asigna automáticamente. La primera llave disponible se usa hasta alcanzar un límite;
            después se prueba la siguiente.
          </small>
        </form>
      )}

      <div className="key-list">
        {(state?.keys ?? []).map((key, index) => (
          <GeminiKeyRow
            active={state?.active_key_id === key.id}
            busy={busy}
            key={key.id}
            llmKey={key}
            order={index + 1}
            onDelete={() => {
              setDeleteId(key.id);
              setDeleteConfirmation("");
            }}
            onTest={() => onTest(key.id)}
            onUpdate={(updates) => onUpdate(key.id, updates)}
          />
        ))}
        {state && !state.keys.length ? <div className="empty-state compact">Todavía no hay llaves registradas.</div> : null}
      </div>

      {deleteKey ? (
        <div className="delete-confirmation">
          <strong>Eliminar {deleteKey.name}</strong>
          <p>Escribe el nombre exacto. Esto no revoca la llave en Google AI Studio.</p>
          <input
            autoFocus
            value={deleteConfirmation}
            onChange={(event) => setDeleteConfirmation(event.target.value)}
          />
          <div className="button-row">
            <button
              className="danger-button compact"
              type="button"
              disabled={busy || deleteConfirmation !== deleteKey.name}
              onClick={async () => {
                const deleted = await onDelete(deleteKey.id, deleteConfirmation);
                if (deleted) {
                  setDeleteId(null);
                  setDeleteConfirmation("");
                }
              }}
            >
              Eliminar definitivamente
            </button>
            <button className="ghost-button compact" type="button" onClick={() => setDeleteId(null)}>
              Cancelar
            </button>
          </div>
        </div>
      ) : null}

      {state?.audit.length ? (
        <details className="key-audit">
          <summary>Auditoría reciente</summary>
          {state.audit.slice(0, 8).map((item) => (
            <p key={item.id}>
              <strong>{item.credential_name}</strong> · {auditLabel(item.action)} · {item.actor} · {formatDate(item.created_at)}
            </p>
          ))}
        </details>
      ) : null}
    </section>
  );
}

function GeminiKeyRow({
  llmKey,
  order,
  active,
  busy,
  onUpdate,
  onTest,
  onDelete
}: {
  llmKey: LlmKey;
  order: number;
  active: boolean;
  busy: boolean;
  onUpdate: (updates: KeyUpdates) => Promise<void>;
  onTest: () => Promise<void>;
  onDelete: () => void;
}) {
  return (
    <article className={`key-item ${active ? "active" : ""}`}>
      <div className="key-title">
        <div>
          <strong>{llmKey.name}</strong>
          <span>{active ? "En uso" : statusLabel(llmKey.status)} · Orden automático {order}</span>
        </div>
        <span className={`key-status ${llmKey.status}`}>{statusLabel(llmKey.status)}</span>
      </div>
      <div className="key-usage-grid">
        <UsageMetric label="Solicitudes" value={formatNumber(llmKey.request_count)} />
        <UsageMetric label="Tokens" value={formatNumber(llmKey.total_tokens)} />
        <UsageMetric label="Coste estimado" value={formatUsd(llmKey.estimated_cost_usd)} />
        <UsageMetric label="Límites 429" value={formatNumber(llmKey.quota_exhaustion_count)} />
      </div>
      <TtftVisualization llmKey={llmKey} />
      <div className="key-meta">
        <span>{llmKey.error_count} errores observados</span>
        <span>
          {llmKey.last_generation_at
            ? `Última generación ${formatDate(llmKey.last_generation_at)}`
            : "Sin solicitudes todavía"}
        </span>
      </div>
      <small>Uso observado por Coordina; Google aplica la cuota total al proyecto asociado.</small>
      {llmKey.cooldown_until ? <small>En espera hasta {formatDate(llmKey.cooldown_until)}</small> : null}
      {llmKey.last_error ? <small className="key-error">{llmKey.last_error}</small> : null}
      {llmKey.managed ? (
        <div className="key-actions">
          <button className="ghost-button compact" type="button" disabled={busy} onClick={onTest}>
            Probar
          </button>
          <button
            className="ghost-button compact"
            type="button"
            disabled={busy}
            onClick={() => onUpdate({ enabled: !llmKey.enabled })}
          >
            {llmKey.enabled ? "Desactivar" : "Activar"}
          </button>
          <button className="danger-button compact" type="button" disabled={busy} onClick={onDelete}>
            Eliminar
          </button>
        </div>
      ) : (
        <small>Llave heredada del entorno; configura la llave maestra para migrarla.</small>
      )}
    </article>
  );
}

function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TtftVisualization({ llmKey }: { llmKey: LlmKey }) {
  const latest = llmKey.last_ttft_ms;
  if (latest === null) {
    return (
      <div className="ttft-card empty">
        <div>
          <span>Tiempo al primer token (TTFT)</span>
          <strong>Sin mediciones</strong>
        </div>
        <small>Se medirá al recibir el primer fragmento de una solicitud real a Gemini.</small>
      </div>
    );
  }

  const markerPercent = Math.min(100, (latest / 4000) * 100);
  const level = latest <= 1000 ? "fast" : latest <= 2500 ? "medium" : "slow";

  return (
    <div className={`ttft-card ${level}`}>
      <div className="ttft-head">
        <div>
          <span>Tiempo al primer token (TTFT)</span>
          <strong>{formatDuration(latest)}</strong>
        </div>
        <small>Última solicitud · menor es mejor</small>
      </div>
      <div
        aria-label={`Último TTFT ${formatDuration(latest)}`}
        className="ttft-track"
        role="img"
      >
        <span className="ttft-marker" style={{ left: `${markerPercent}%` }} />
      </div>
      <div className="ttft-axis" aria-hidden="true">
        <span>0</span>
        <span>1 s</span>
        <span>2,5 s</span>
        <span>4 s+</span>
      </div>
      <div className="ttft-stats">
        <span>Promedio <strong>{formatDuration(llmKey.average_ttft_ms ?? latest)}</strong></span>
        <span>Mejor <strong>{formatDuration(llmKey.best_ttft_ms ?? latest)}</strong></span>
        <span>Peor <strong>{formatDuration(llmKey.worst_ttft_ms ?? latest)}</strong></span>
        <span>Muestras <strong>{formatNumber(llmKey.ttft_sample_count)}</strong></span>
      </div>
    </div>
  );
}

function statusLabel(status: LlmKey["status"]) {
  return {
    unverified: "Sin verificar",
    ready: "Disponible",
    cooldown: "Límite alcanzado",
    invalid: "Inválida",
    incompatible: "Modelo no disponible",
    disabled: "Desactivada"
  }[status];
}

function auditLabel(action: string) {
  return {
    created: "creada",
    updated: "actualizada",
    tested: "probada",
    deleted: "eliminada",
    legacy_imported: "migrada"
  }[action] ?? action;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("es-CL", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("es-CL").format(value);
}

function formatUsd(value: number) {
  return new Intl.NumberFormat("es-CL", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 4,
    maximumFractionDigits: 6
  }).format(value);
}

function formatDuration(value: number) {
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 2 : 1).replace(".", ",")} s`;
}
