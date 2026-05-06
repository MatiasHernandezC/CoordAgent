EXTRACTION_PROMPT = """
Extrae disponibilidad desde el mensaje del usuario.

Devuelve SOLO JSON con esta forma:

{
  "participants": [
    {
      "name": "string",
      "availability": [
        {
          "day": "lunes|martes|miercoles|jueves|viernes",
          "start": "HH:MM",
          "end": "HH:MM"
        }
      ]
    }
  ]
}

Reglas:
- No inventes participantes.
- No inventes dias si no aparecen.
- Si dice "manana", usa 09:00-12:00.
- Si dice "tarde", usa 15:00-18:00.
- Si dice "desde las 16", usa 16:00-18:00.
- Si dice "cualquier hora", usa 09:00-18:00.
- Si dice "todos los dias", crea disponibilidad para lunes, martes, miercoles, jueves y viernes.
- Si falta horario, deja availability vacio.
- Responde solo JSON, sin markdown.
"""
