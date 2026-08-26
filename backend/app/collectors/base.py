import json
from abc import ABC, abstractmethod

from app.schemas.content import Content


def to_json_safe(value):
    """dict/list 등에 섞인 datetime, time.struct_time 등 JSON으로 바로 안 되는 값을
    문자열로 변환한다 (raw_data는 JSONB 컬럼에 그대로 들어가야 하므로)."""
    return json.loads(json.dumps(value, default=str))


class BaseCollector(ABC):
    source_name: str

    @abstractmethod
    def collect(self) -> list[Content]:
        """소스에서 원본 데이터를 가져와 공통 Content 스키마로 변환해 반환한다.
        실패 시 예외를 던진다 — 호출부(run_pipeline)가 소스 단위로 격리 처리한다."""
        raise NotImplementedError
