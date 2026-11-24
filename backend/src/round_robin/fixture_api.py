"""Read-only localhost fixture surface. Production identity and inference are absent."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .fixtures import SampleBundle, sample_bundle
from .schemas import DraftBrief

app = FastAPI(title="Round Robin offline fixture API", version="0.1.0", description="Fictional read-only Phase 0 preparation. No mutation, paid access or provider calls.")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    if any(request.headers.get(name) for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
        return JSONResponse({"detail": "External proxy exposure is disabled for the offline fixture API."}, status_code=403)
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(origin)
            valid = (not any(ord(c) <= 32 or ord(c) == 127 for c in origin)
                     and "\\" not in origin and parsed.scheme in {"http", "https"}
                     and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.path in {"", "/"} and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            return JSONResponse({"detail": "Only loopback origins may read the offline fixture API."}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/sample", response_model=SampleBundle)
def read_sample() -> SampleBundle:
    return sample_bundle()


@app.get("/api/schemas/brief")
def read_brief_schema() -> dict:
    return DraftBrief.model_json_schema(mode="serialization")


@app.get("/api/schemas/sample")
def read_sample_schema() -> dict:
    return SampleBundle.model_json_schema(mode="serialization")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "offline_fixture", "paid_access_enabled": False}
