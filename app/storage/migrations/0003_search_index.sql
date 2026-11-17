-- 0003_search_index.sql
-- 이벤트 텍스트 검색 색인 (계획서 §2.2 SQLite FTS5, §6.3 계층 2 "텍스트 검색", §9.1 "검색 인덱스 재생성 지원").
-- events를 원본으로 하는 외부 내용(external content) 색인이다. 색인은 언제든 'rebuild'로 다시 만들 수 있다.
-- '-'와 '_'를 단어 글자로 취급해 TEST-E001 같은 코드가 한 토큰으로 남게 한다.

CREATE VIRTUAL TABLE events_fts USING fts5(
    message,
    event_type,
    error_code,
    content = 'events',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2 tokenchars '-_'"
);

CREATE TRIGGER events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts (rowid, message, event_type, error_code)
    VALUES (NEW.id, NEW.message, NEW.event_type, NEW.error_code);
END;

CREATE TRIGGER events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts (events_fts, rowid, message, event_type, error_code)
    VALUES ('delete', OLD.id, OLD.message, OLD.event_type, OLD.error_code);
END;

CREATE TRIGGER events_fts_update AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts (events_fts, rowid, message, event_type, error_code)
    VALUES ('delete', OLD.id, OLD.message, OLD.event_type, OLD.error_code);
    INSERT INTO events_fts (rowid, message, event_type, error_code)
    VALUES (NEW.id, NEW.message, NEW.event_type, NEW.error_code);
END;

INSERT INTO events_fts (events_fts) VALUES ('rebuild');
