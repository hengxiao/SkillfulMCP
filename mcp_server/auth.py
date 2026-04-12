from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from jose import jwt, JWTError

from .config import get_settings
from .models import Agent


def issue_token(agent: Agent, expires_in: int = 3600) -> str:
    """Sign and return a JWT for the given agent."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": agent.id,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "skillsets": agent.skillsets or [],
        "skills": agent.skills or [],
        "scope": agent.scope or [],
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def validate_token(token: str) -> dict[str, Any]:
    """Validate signature, expiry, and issuer. Raise HTTP 401 on any failure."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": True},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if claims.get("iss") != settings.jwt_issuer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token issuer mismatch",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims
