# ☂️ Keisung Umbrella — Server MVP v0.3

학교 공용 우산 대여·반납 관리 프로젝트. FastAPI + SQLite 서버 기반이며, **GitHub Pages 단독으로는 작동하지 않습니다.**

## 화면 구성

- `/` — **16:9 키오스크/전자칠판용 학생 화면**, 전체 뷰포트 사용, 큰 QR 스캔·선택·대여·반납 버튼
- `/admin` — 별도 관리자 로그인 및 관리 화면
- `/api/...` — 서버에서 기록 검증·저장

## Codespaces 실행 (Linux)

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

그다음 `.env`를 열어 **ADMIN_PASSWORD를 실제 강력한 비밀번호로 변경**하세요. (최소 12자, 저장소에 커밋하지 마세요.)

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=본인이_직접_정한_강력한_비밀번호
APP_ORIGIN=
SECURE_COOKIES=true
DATA_DIR=./data
```

`APP_ORIGIN`은 기본적으로 비워둡니다. Codespaces 프록시가 원래 Host를 전달하지 않아 로그인에서 **"요청 출처가 올바르지 않습니다"**가 뜬다면, Codespaces **PORTS** 탭에 있는 **실제 HTTPS 주소**를 그대로 넣으세요(끝에 `/` 없이).

```dotenv
APP_ORIGIN=https://YOUR-CODESPACE-8000.app.github.dev
```

서버 실행:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Codespaces Ports에서 **8000 포트를 Private**으로 유지하고 Forwarded Address를 열어보세요. 메인 주소는 학생 키오스크, `/admin`은 관리자 로그인 화면입니다.

**주의:** 예전에 같은 셸에서 `export ADMIN_USERNAME`, `export ADMIN_PASSWORD`, `export APP_ORIGIN`을 사용했다면 환경변수가 `.env`보다 우선합니다. 새 Codespaces 터미널을 열거나 아래를 실행하세요.

```bash
unset ADMIN_USERNAME ADMIN_PASSWORD APP_ORIGIN SECURE_COOKIES DATA_DIR
```

그다음 서버를 재시작하면 `.env` 값이 적용됩니다. v0.3부터 시작 시 `.env` 관리자 계정 정보가 SQLite 관리자 계정에 반영되고 이전 세션은 만료됩니다.

### HTTP 401/403에 관하여

- `GET /api/admin/me`의 **401**은 로그인 전 정상 동작입니다.
- 로그인 **403**은 브라우저 Origin과 실제 공개 Host/설정된 `APP_ORIGIN`이 맞지 않는 경우입니다.
- 로그인 **401**은 비밀번호 불일치 등 인증 실패입니다.
- 로그인 성공 뒤 브라우저에서 쿠키가 유지되지 않으면 HTTPS 주소와 `SECURE_COOKIES`를 확인하세요.

## 우산 상태와 이력

- `UMB-0001`부터 등록 순서대로 고유 ID 증가. 삭제·ID 재사용 기능 없음.
- 우산당 대여 1건, 학생당 동시 대여 1개. SQLite 트랜잭션으로 동시 요청 검증.
- 반납에는 학년·반·번호 일치 필요. **학년·반·번호는 본인 인증이 아닙니다.**
- 관리자 상태: `LOST`(분실), `DAMAGED`(파손), `DISPOSED`(폐기). 분실·파손 복구 가능, 폐기 복구 불가.
- 이벤트는 SQLite DB에 추가되며 관리자 `/api/admin/export`에서 JSON 백업 가능합니다.
- 브라우저 QR 스캔은 BarcodeDetector 지원 및 카메라 HTTPS 권한이 필요합니다. 지원하지 않으면 ID 수동 입력.

## 파일

```text
server.py          # FastAPI, SQLite, 로그인, 이벤트 기록
kiosk.html         # 16:9 학생용 전체 화면
index.html         # /admin 관리자 화면
.env.example       # 복사하여 .env 생성
requirements.txt
Dockerfile
.gitignore
```

## 배포 전 필수 검토

- 학생 학년/반/번호는 개인정보가 될 수 있으므로 실제 운영 전 학교 승인·보호조치·보존 기간이 필요합니다.
- 현재 학생 본인 인증이 없어 타인이 다른 학생 정보로 대여/반납할 수 있습니다. **운영용 인증 연동이 필요합니다.**
- 관리자 로그인 시도 제한, 계정 관리/비밀번호 변경, 운영 로그 모니터링 등은 추가 필요합니다.
- 외부 서버는 HTTPS 필수, `SECURE_COOKIES=true` 사용. `DATA_DIR`는 영구 저장 볼륨이어야 합니다.
- `.env`, SQLite DB, JSON 백업을 공개 GitHub에 업로드하지 마세요.
- 기존 브라우저 `localStorage` v0.1 기록은 서버로 자동 마이그레이션하지 않습니다.
