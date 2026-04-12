from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .auth import validate_token
from .config import get_settings

_bearer = HTTPBearer(auto_error=True)


def get_db(request: Request) -> Session:
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_current_claims(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    return validate_token(credentials.credentials)


def require_admin(x_admin_key: str = Header(default="")) -> None:
    settings = get_settings()
    if settings.admin_key and x_admin_key != settings.admin_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Admin-Key header",
        )
