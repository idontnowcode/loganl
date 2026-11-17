"""공용 데이터 모델. 다른 app 패키지에 의존하지 않는다."""

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """DB에 저장하는 시각 형식: UTC ISO-8601, 밀리초, 끝에 Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
