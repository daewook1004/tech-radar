import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings

_security = HTTPBasic(auto_error=False)


def require_basic_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    """basic_auth_user/password가 둘 다 설정된 경우에만 인증을 요구한다.
    비어있으면(로컬 개발 기본값) 통과 — deliver.py의 _default_sender()와 같은 폴백 패턴."""
    settings = get_settings()
    if not settings.basic_auth_user or not settings.basic_auth_password:
        return

    valid = credentials is not None and secrets.compare_digest(
        credentials.username, settings.basic_auth_user
    ) and secrets.compare_digest(credentials.password, settings.basic_auth_password)

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
