import type { Day } from "./types";

export const EXAMPLE =
  "Yo puedo lunes en la tarde, Camila puede lunes desde las 16 y Diego puede martes en la manana, Pedro puede a cualquier hora todos los dias";

export const CHANNEL_EXAMPLE = [
  { sender: "Nicolas", text: "yo puedo lunes en la tarde" },
  { sender: "Camila", text: "yo puedo lunes desde las 16" },
  { sender: "Diego", text: "yo puedo martes en la manana" },
  { sender: "Pedro", text: "puedo a cualquier hora todos los dias" },
  { sender: "Nicolas", text: "@coordina nos ayudas a cerrar un horario?" }
];

export const DAYS: Day[] = ["lunes", "martes", "miercoles", "jueves", "viernes"];
export const HOURS = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00"];
