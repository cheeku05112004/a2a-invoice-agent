import hashlib
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme)
):
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required"
        )

    token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required"
        )

    # Each Bearer token represents a separate user.
    principal = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()

    return principal

