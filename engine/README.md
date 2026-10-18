# Moru Engine

DSPy 기반 Minecraft 모드팩 번역 엔진 + FastAPI 로컬 사이드카 서버.
데스크톱 앱이 스폰하는 사이드카로 동작하며, CLI로 단독 실행도 가능하다.

## 설치

Python 3.13+, [uv](https://docs.astral.sh/uv/) 필요.

```bash
uv sync
```

## CLI

```bash
# 모드팩 스캔 (번역 대상 파일 탐색)
uv run python -m moru_engine.cli scan ./test/modpack

# 번역 실행
uv run python -m moru_engine.cli translate ./test/modpack --model openai/gpt-5.6-luna
```

`--source`/`--target`으로 로케일(기본 `en_us` → `ko_kr`), `--api-base`로
Ollama 등 로컬 엔드포인트를 지정한다. API 키는 프로바이더 환경 변수로 전달.

## 서버 모드 (데스크톱 사이드카)

```bash
uv run python -m moru_engine.server --port 43110 --token <세션토큰>
```

127.0.0.1 전용. 데스크톱 앱이 빈 포트와 세션 토큰을 정해 스폰한다.
API 계약은 `../contracts/engine-api.yaml`.

## 테스트

```bash
uv run pytest
```

## tools/ (오퍼레이터 도구 — 앱 번들에 포함되지 않음)

- `optimize.py` — GEPA로 번역 프로그램을 컴파일해 아티팩트 JSON 생성 (릴리스에 번들)
- `evaluate.py` — 현재 프로그램/아티팩트의 테스트 스플릿 점수 기록
- `build_evalset.py` — 평가셋(바닐라 + 스트레스 케이스 + modtext 골드셋) 스냅샷 생성
- `harvest_modpack_pairs.py` — 사람이 번역한 모드팩(zip/디렉토리)에서 문장 단위
  골드셋 추출 → `optimize.py`/`evaluate.py`의 `--modtext` 입력
- `build_vanilla_glossary.py` — 바닐라 공식 번역에서 용어집 생성

## 구조

- `src/moru_engine/scanner/` — 모드팩 스캔·파일 페어링, 팩 식별
- `src/moru_engine/parsers/` — 포맷별 파서 (JSON, JSON5, lang, SNBT, NBT, XML 등)
- `src/moru_engine/handlers/` — 콘텐츠별 핸들러 (FTB Quests SNBT·JSON5, Patchouli, Origins 등)
- `src/moru_engine/dspy_modules/` — DSPy 번역 모듈, LM 팩토리, 컴파일 아티팩트 로딩
- `src/moru_engine/pipeline/` — 오케스트레이터: 스캔 → TM/용어집 → 번역 → 검증 → 출력
- `src/moru_engine/validator/` — 플레이스홀더·용어집·포맷 검증
- `src/moru_engine/tm/` — SQLite 로컬 번역 메모리
- `src/moru_engine/glossary/` — 용어 마이닝, 바닐라 용어집 빌더
- `src/moru_engine/evalset/` — 평가셋 빌더·메트릭
- `src/moru_engine/output/` — 번역 결과 라우팅·리소스팩 생성
- `src/moru_engine/server/` — FastAPI 사이드카 (잡 관리, 업로드)
- `packaging/` — PyInstaller onedir 스펙 (사이드카 배포용)
