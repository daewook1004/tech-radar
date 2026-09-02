from fastapi import FastAPI

from app.web.router import router as web_router

app = FastAPI(title="Tech Radar")
app.include_router(web_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
