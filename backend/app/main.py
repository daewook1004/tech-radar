from fastapi import FastAPI

app = FastAPI(title="Tech Radar")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
