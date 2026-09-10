import os

# Los tests usan el repositorio JSON en memoria/disco, no PostgreSQL. Sin esto,
# quien clona el repo y corre pytest en limpio obtiene errores de conexion
# porque DB_BACKEND por defecto es "postgres" (ver app/settings.py).
os.environ.setdefault("DB_BACKEND", "json")
