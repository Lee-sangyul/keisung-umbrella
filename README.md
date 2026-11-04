# ☂️ Keisung Umbrella — Server MVP v0.8

학교 공용 우산 대여·반납 관리 프로젝트. FastAPI + SQLite 서버 기반이며, **GitHub Pages 단독으로는 작동하지 않습니다.**

## 화면 구성

- `/` — **16:9 터치 키오스크**, 대여/반납 선택 → 학년·반·번호 숫자 타일 선택 → QR 스캔/ID 입력 → 최종 확인 → 완료 (테이블링의 현장 등록식 단계형 UX 참고)
- `/admin` — 별도 관리자 로그인 및 관리 대시보드 (학생별 검색, 우산 상태 관리, 이력)
- `/admin/print` — **관리자 전용 QR 인쇄 센터:** 단일/일괄 인쇄, 우산 ID 검색·상태 필터, A4/Letter, 가로/세로, 열·행·여백·QR 크기·제목·ID 표시·테두리 설정
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

**v0.4 변경:** `python-dotenv`가 `.env` 값을 기존 셸 `export` 값보다 우선 적용합니다. 아래 명령은 이전 설정을 정리하고 싶을 때만 선택적으로 실행하세요.

```bash
unset ADMIN_USERNAME ADMIN_PASSWORD APP_ORIGIN SECURE_COOKIES DATA_DIR
```

이제 서버 재시작 시 `.env` 관리자 계정 정보가 SQLite에 반영되고 이전 세션은 만료됩니다. `ADMIN_PASSWORD`는 **실제 비밀번호**여야 하고 `CHANGE_ME` 예시값은 사용할 수 없습니다. 서버 재시작 후 `/admin`에 `.env`의 `ADMIN_USERNAME` 및 `ADMIN_PASSWORD`와 정확히 같은 값을 입력하세요.

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

## 로그인 401 오류 해결 순서

1. `git pull`, `pip install -r requirements.txt` 실행
2. `.env`를 열어 `ADMIN_USERNAME`, `ADMIN_PASSWORD`를 실제 값으로 수정 (예시 `CHANGE_ME...` 사용 금지)
3. Uvicorn을 완전히 종료(Ctrl+C)한 뒤 다시 시작
4. Codespaces의 **Ports** 탭에서 포트 8000의 HTTPS 주소 접속 → `/admin`
5. 로그인 필드에 `.env`의 값과 **완전히 같은** ID/비밀번호 입력. 앞뒤 공백이나 기존에 브라우저가 저장한 암호 자동완성 확인

`/api/admin/me` 요청의 로그인 전 401은 정상이고, `POST /api/admin/login`의 401은 계정/비밀번호 불일치입니다. 로그인 요청에서 403이면 `APP_ORIGIN`과 Codespaces 접속 URL을 확인하세요.

**보안:** Codespaces 포트는 Private으로 유지하고 비밀번호, `.env`, DB를 공유하거나 커밋하지 마세요. 학생 본인 인증이 없어 실사용 전 인증 보완이 필요합니다.

## QR 라벨 인쇄 (v0.8)

관리자 로그인 후 `/admin/print`로 이동하세요. `/admin`의 우산 관리 목록에서 `QR 인쇄`를 누르면 해당 우산만 선택됩니다. `QR 일괄 인쇄`로 여러 우산을 선택하거나 상태별 필터·검색으로 일괄 선택할 수 있습니다.

- `segno` 패키지가 서버에서 실제 스캔 가능한 QR SVG를 생성합니다. QR에는 **우산 ID만** 담고 학생 정보는 포함되지 않습니다.
- 인쇄 설정: A4/Letter, 세로/가로, 열(1~4), 행(2~8), QR 크기(20~50mm), 여백(5~20mm), 제목/ID/절취선 표시 여부.
- QR 생성 API `POST /api/admin/qr-labels`는 **관리자 세션 및 CSRF 검증**을 거치고 등록된 우산 ID만 처리합니다. 인쇄 UI는 요청을 200개씩 나눠 처리하며 최대 1,000개까지 지원합니다.
- 인쇄 창에서 미리보기 확인 후 **인쇄 시작**을 클릭합니다. 프린터 배율 100%, 브라우저 머리글/바닥글 끄기 권장. 최초 인쇄는 시험 출력 후 실제로 QR 인식되는지 확인하세요.
- 요청한 크기가 라벨 안에 들어가지 않으면 인쇄를 막고 설정을 조정하라는 메시지를 표시합니다. 실제 라벨지 위치 정합성은 프린터/용지에 따라 달라질 수 있습니다.

기존 서버를 종료한 뒤 `git pull origin main && pip install -r requirements.txt` 후 Uvicorn을 다시 시작해야 `segno` 의존성이 적용됩니다. 

## QR 스캔 (브라우저 호환)

학생용 `/` → 대여 또는 반납 → 학생 정보 선택 → **QR 스캔 시작**을 클릭하세요. 브라우저의 카메라 접근을 허용하면 QR 스캔 팝업에서 인식할 수 있고, 성공하면 우산 ID (`UMB-0001` 형식)가 자동 입력됩니다.

- 브라우저에서 `BarcodeDetector`를 지원하면 우선 사용하며, 없거나 오류가 발생하면 저장소에 포함된 `assets/jsQR.js`로 QR을 판독합니다. 외부 CDN 없이 동작합니다.
- 카메라를 사용할 수 없다면 우산 ID를 수동 입력할 수 있습니다. **QR 사진 업로드 및 이미지 파일 인식 기능은 제공하지 않습니다.**
- 스마트폰은 HTTPS에서 카메라 권한을 허용해야 합니다. `http://localhost` 등 허용되는 로컬 개발 환경 외의 일반 HTTP 주소에서는 카메라를 열 수 없습니다.
- 저장소 내 `jsQR`은 Apache-2.0 라이선스의 오픈소스 라이브러리이며 `assets/jsQR-LICENSE.txt`를 함께 제공합니다.
- 스캔은 QR에 담긴 우산 ID 형식만 검증합니다. 학생 정보 인증과 대여 가능 여부는 기존 서버 API에서 처리됩니다.

