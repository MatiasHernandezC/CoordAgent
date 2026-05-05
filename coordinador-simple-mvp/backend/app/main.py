from fastapi import FastAPI

app = FastAPI(title="Coordinador Simple MVP", version="0.1.0")


@app.get("/health")
def health():
    return {"ok": True, "service": "coordinador-simple-mvp"}
