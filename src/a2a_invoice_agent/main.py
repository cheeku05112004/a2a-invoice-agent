
import os

from dotenv import load_dotenv

load_dotenv()

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "http://127.0.0.1:8000/a2a/"
).rstrip("/") + "/"

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from a2a_invoice_agent.api import router
from a2a_invoice_agent.database import init_db


app = FastAPI(
    title="A2A Invoice Agent",
    description="AI-powered invoice decision agent",
    version="1.0.0"
)

@app.exception_handler(HTTPException)
async def log_http_errors(request: Request, exc: HTTPException):
    if exc.status_code == 400:
        print(
            "DIAGNOSTIC HTTP 400:",
            request.method,
            request.url.path,
            "DETAIL:",
            exc.detail,
            flush=True,
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )

@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/")
def home():
    return {
        "message": "A2A Invoice Agent is running!",
        "status": "success"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.get("/.well-known/agent-card.json")
def agent_card():
    return JSONResponse(
        content={
            "name": "A2A Invoice Agent",
            "description": "AI-powered invoice decision agent",
            "version": "1.0.0",
            "supportedInterfaces": [
                {
                    "url": PUBLIC_BASE_URL,
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0"
                }
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False
            },
            "defaultInputModes": [
                "application/vnd.ga5.invoice-claim-batch+json"
            ],
            "defaultOutputModes": [
                "application/vnd.ga5.invoice-action-proposals+json",
                "application/vnd.ga5.invoice-action-receipts+json"
            ],
            "skills": [
                {
                    "id": "invoice_action_agent",
                    "name": "Invoice Action Agent",
                    "description": "Analyzes invoice packages and proposes safe business actions.",
                    "tags": [
                        "invoice",
                        "payment",
                        "approval",
                        "reconciliation"
                    ]
                }
            ]
        }
    )


app.include_router(router)
