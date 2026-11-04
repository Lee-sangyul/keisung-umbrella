# ☂️ Keisung Umbrella — Server MVP v0.2

학교 공용 우산 대여·반납 관리 웹앱. **GitHub Pages 전용 정적 앱이 아닌 Python FastAPI 서버**로 실행합니다.

## URL

- `/` — 학생용 우산 QR 인식, 학년·반·번호 입력 후 대여/반납
- `/admin` — 관리자 계정 로그인, 우산 등록, 분실·파손·폐기 처리, 로그 조회, JSON 백업
- 서버 JSON API — `/api/umbrellas`, `/api/borrow`, `/api/return`, `/api/admin/*`

## 구현 범위

- SQLite WAL 데이터베이스로 서버에 우산과 학생별 대여 내역 저장
- `BEGIN IMMEDIATE` 트랜잭션으로 동시 대여 시 중복 대여 차단
- 학생은 한 번에 1개, 우산은 `AVAILABLE`일 때만 대여
- 반납 시 대여자 학년·반·번호 일치 확인 (**이 값은 본인 인증이 아님**)
- `UMB-0001`부터 SQLite 자동 증가 ID, 등록 우산 삭제 기능 없음
- 관리자: 분실(`LOST`), 파손(`DAMAGED`), 폐기(`DISPOSED`) 처리
- 분실·파손은 관리자 복구 가능, 폐기 상태는 변경 불가
- 서버 이벤트 로그 감사 기록 및 관리자 전용 JSON 백업
- 관리자 ID/비밀번호: 환경변수로 초기 구성, scrypt 해시 및 HttpOnly 세션 쿠키, CSRF 검증

## 로컬 실행 (Python 3.10+)

```bash
python -m venv .venv

# Windows
.venv\\Scripts\\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# .env.example 파일의 이름을 .env 로 변경한 뒤 알맞은 값을 넣은 후 저장

uvicorn server:app --host 127.0.0.1 --port 8000
```

브라우저에서 `http://127.0.0.1:8000/` 및 `http://127.0.0.1:8000/admin`을 엽니다. 최초 실행시 관리자 계정이 생성됩니다. **최초 설정 이후 환경변수만 수정해도 기존 계정 비밀번호는 바뀌지 않습니다.** 비밀번호 변경 UI는 아직 없으며 DB 마이그레이션이 필요합니다.

## 서버 배포

FastAPI를 실행하는 Python/Docker 호스트가 필요합니다. GitHub Pages는 Python 서버/SQLite를 실행하지 못합니다.

- 프로덕션 서버에는 **HTTPS 도메인**을 연결하고 `SECURE_COOKIES=true`, `APP_ORIGIN=https://실제-도메인`을 설정하세요.
- `DATA_DIR`를 **영구 볼륨**으로 연결하세요. 배포 때마다 데이터가 없어지는 임시 파일시스템은 사용하면 안 됩니다.
- 관리자 비밀번호를 GitHub 저장소, 커밋, README, 클라이언트 JS에 넣지 마세요.
- 서버 프로세스는 가급적 단일 인스턴스로 시작하고, SQLite 파일은 로컬 영구 디스크에 두세요.
- 공개된 GitHub Pages 주소가 있으면 이전 정적 앱이 열릴 수 있습니다. **학교에 안내할 주소는 새 서버 주소**입니다.

## 운영 보안 / 미구현 사항

**현재는 서버형 프로토타입이며 학교 실사용 승인을 받은 제품이 아닙니다.**

- 학생의 학년·반·번호 입력은 본인 인증이 아닙니다. 타인의 정보를 입력한 대여·반납을 막으려면 학교 계정 인증 또는 교사 확인이 필요합니다.
- 공용 QR에는 우산 ID만 포함하고, 개인정보는 JSON 백업 및 SQLite DB에만 접근제어하여 보관하세요.
- 로그인 시도 횟수 제한, 계정 잠금, 관리자 비밀번호 변경·복구, 계정별 역할, 데이터 보존·삭제 정책, 정기 백업, 서버 모니터링, 감사 로그 보호는 운영 전 추가가 필요합니다.
- 공개 GitHub 저장소에는 학생 학번/대여 이력/DB/JSON 백업을 업로드하지 마세요.
- 기존 v0.1 브라우저 `localStorage` 테스트 데이터는 자동 이전되지 않습니다.
- 카메라 스캔은 브라우저 `BarcodeDetector` 지원 여부에 따라 동작하며, 미지원 시 수동 ID 입력 가능합니다.

## 프로젝트 파일

```text
index.html         # 학생용 + /admin 프론트엔드
server.py          # FastAPI / SQLite / 관리자 세션 API
requirements.txt   # Python 의존성
Dockerfile         # 서버 배포 이미지
.gitignore         # DB 및 비밀정보 제외
```
