# Arquitectura simple

## Vista general

```mermaid
flowchart LR
  U["Usuario"] --> F["React + TypeScript"]
  F --> API["FastAPI BFF"]
  API --> LLM["LLM Service"]
  API --> SVC["Session Service"]
  SVC --> ENG["Decision Engine"]
  SVC --> JSON["sessions.json"]
```

## Secuencia principal

```mermaid
sequenceDiagram
  actor U as Usuario
  participant F as Frontend
  participant API as FastAPI
  participant L as LLM Service
  participant E as Decision Engine
  participant R as JSON Repository

  U->>F: Crea sesion
  F->>API: POST /api/sessions
  API->>R: Guardar sesion
  API-->>F: session

  U->>F: Escribe disponibilidad
  F->>API: POST /api/sessions/{id}/message
  API->>L: Extraer JSON
  L-->>API: Participantes + horarios
  API->>R: Guardar datos + historial
  API-->>F: session actualizada

  U->>F: Corrige dato manual si hace falta
  F->>API: POST /api/sessions/{id}/availability
  API->>R: Guardar disponibilidad
  API-->>F: session actualizada

  U->>F: Calcular
  F->>API: POST /api/sessions/{id}/calculate
  API->>E: Cruzar horarios
  E-->>API: Top opciones + matriz de disponibilidad
  API->>R: Guardar opciones
  API-->>F: opciones
```

## Endpoints

| Metodo | Ruta | Uso |
| --- | --- | --- |
| GET | `/health` | Verificar backend. |
| POST | `/api/sessions` | Crear sesion. |
| GET | `/api/sessions/{id}` | Obtener sesion. |
| POST | `/api/sessions/{id}/message` | Extraer disponibilidad con LLM. |
| POST | `/api/sessions/{id}/participants` | Agregar participante manualmente. |
| POST | `/api/sessions/{id}/availability` | Agregar disponibilidad manual. |
| POST | `/api/sessions/{id}/calculate` | Calcular mejores horarios. |
| POST | `/api/sessions/{id}/confirm` | Confirmar opcion. |

## Modelo minimo

```txt
Session
  id
  title
  participants
  options
  availability_matrix
  missing_info
  insights
  messages
  selected_option
  decision_summary
  status

Participant
  id
  name
  availability

TimeSlot
  day
  start
  end

TimeOption
  id
  day
  start
  end
  available_participants
  unavailable_participants
  score
  coverage_percent
  explanation

AvailabilityCell
  day
  start
  end
  available_participants
  unavailable_participants
  score
  coverage_percent

ChatMessage
  role
  content
  source
  created_at
```
