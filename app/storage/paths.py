"""데이터 폴더 구조 (계획서 §9).

```
<root>/database/knowledge.db
<root>/cases/normal, cases/abnormal
<root>/attachments/guides, attachments/references (CR-04 참고 자료)
<root>/indexes, analysis, backups, logs
<bootstrap>/config/settings.json   ← 설정은 항상 부트스트랩 폴더 (REQUIREMENTS D-1)
```
기본값에서는 <root>와 <bootstrap>이 같은 폴더라 계획서 그림과 똑같다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    config_dir: Path

    @property
    def database_dir(self) -> Path:
        return self.root / "database"

    @property
    def db_file(self) -> Path:
        return self.database_dir / "knowledge.db"

    @property
    def cases_normal(self) -> Path:
        return self.root / "cases" / "normal"

    @property
    def cases_abnormal(self) -> Path:
        return self.root / "cases" / "abnormal"

    @property
    def guides(self) -> Path:
        return self.root / "attachments" / "guides"

    @property
    def references(self) -> Path:
        return self.root / "attachments" / "references"

    @property
    def indexes(self) -> Path:
        return self.root / "indexes"

    @property
    def analysis(self) -> Path:
        return self.root / "analysis"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def all_dirs(self) -> tuple[Path, ...]:
        return (
            self.database_dir,
            self.cases_normal,
            self.cases_abnormal,
            self.guides,
            self.references,
            self.indexes,
            self.analysis,
            self.backups,
            self.logs,
            self.config_dir,
        )

    def ensure(self) -> None:
        """없는 폴더를 만든다. 여러 번 불러도 안전하다."""
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
