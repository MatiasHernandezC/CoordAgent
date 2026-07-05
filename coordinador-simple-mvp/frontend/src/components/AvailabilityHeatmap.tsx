import type { AvailabilityCell } from "../types";
import { DAYS, HOURS } from "../constants";
import { getHeatLevel } from "../format";

export function AvailabilityHeatmap({
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
