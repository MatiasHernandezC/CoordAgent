export function LogoMark() {
  return (
    <div className="logo-mark" aria-hidden="true">
      <span className="logo-node node-a" />
      <span className="logo-node node-b" />
      <span className="logo-node node-c" />
    </div>
  );
}

export function Metric({ title, value, detail }: { title: string; value: string | number; detail: string }) {
  return (
    <article className="metric-card">
      <span>{title}</span>
      <strong>{value}</strong>
      <p>{detail}</p>
    </article>
  );
}

export function EmptyState({ text }: { text: string }) {
  return <div className="empty-state">{text}</div>;
}
