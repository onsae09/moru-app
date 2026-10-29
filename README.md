# Moru (모루)

Minecraft 모드팩 번역 도구 — DSPy 번역 엔진 + Electron 데스크톱 앱.

웹 커뮤니티: https://moru.gg

## 구조

- `engine/` — Python 3.13 번역 엔진 (DSPy) + FastAPI 로컬 서버. `uv`로 관리.
- `desktop/` — Electron + React 데스크톱 앱. `bun`으로 관리.
- `contracts/` — OpenAPI 계약: `engine-api.yaml` (engine↔desktop), `web-api.yaml` (desktop↔web, 공개 계약 원본).

## 개발

```bash
cd engine
uv sync
uv run python -m moru_engine.cli scan ./test/modpack
uv run pytest
```

## 릴리스

```bash
scripts/bump-version.sh 0.2.0 --tag   # desktop/engine 버전 동기 + 커밋 + v0.2.0 태그
git push origin main v0.2.0
```

태그 push가 `.github/workflows/release.yml`을 트리거 — 각 OS 러너에서
PyInstaller onedir 사이드카 빌드 → `desktop/resources/engine/` 스테이징 →
`/health` 스모크 → electron-builder가 Windows NSIS `.exe`, macOS `.dmg`
(Apple Silicon `arm64`, Intel `x64` 각각), Linux `.AppImage`를 GitHub Release
초안에 게시한다. 초안을 publish해야 electron-updater가 집어간다.

- macOS는 Apple Developer ID 미보유 시 ad-hoc 서명 dmg (우클릭 → 열기 필요,
  자동 업데이트 제외). 서명하려면 `CSC_LINK`/`CSC_KEY_PASSWORD` secrets 추가.
- 두 macOS 잡은 각자 `latest-mac.yml`을 올리므로, macOS 자동 업데이트를 켤 때는
  두 아키텍처 항목을 합친 파일이 필요하다 (지금은 ad-hoc 서명이라 꺼져 있음).
- PR/CI: `.github/workflows/ci.yml` — 엔진 pytest, 데스크톱 typecheck+`bun test`+build,
  PyInstaller 패키징 스모크(`/health` + 번들 데이터 검증).

## 라이선스

MIT
