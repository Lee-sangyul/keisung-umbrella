# Keisung Umbrella ☂️

학교 우산 대여 관리 시스템 MVP — GitHub Pages용 정적 웹 앱.

## 현재 범위

- 우산 ID QR 스캔 (`BarcodeDetector` 지원 브라우저) 및 수동 입력
- 학년·반·번호 입력, 학생당 우산 1개 제한, 대여 중 우산 중복 대여 방지
- 동일 학생 반납, JSON 이벤트 로그
- 관리자 데모 화면: 우산 등록, 분실·파손·폐기, 분실·파손 복구
- 등록 ID는 영구 보존하고 신규 ID는 계속 증가
- JSON 내보내기/가져오기

## 중요: 테스트 전용

**현재 MVP는 운영용 서버가 없습니다.** 모든 데이터는 해당 브라우저 `localStorage`에만 저장되고 다른 기기와 동기화되지 않습니다. 관리자 탭은 **권한 인증이 없는 데모 UI**이므로 학생의 실제 학년·반·번호, 실제 대여 내역을 입력하지 마세요. 공유 PC 또는 학교 운영에 사용해서는 안 됩니다. JSON 내보내기 파일에도 입력된 학생 정보가 들어갑니다.

실제 운영하려면 비공개 서버/DB, 학교 계정 인증, 서버 측 관리자 권한 검증, 동시 대여 트랜잭션, 보존·파기 정책 및 학교 승인 절차가 필요합니다. 공개 GitHub Pages나 공개 저장소에 학생 기록을 저장하면 안 됩니다.

## 실행

`index.html`을 브라우저에서 열거나 GitHub 저장소 Settings → Pages → Deploy from a branch → `main` / `(root)`를 설정하세요. 카메라 스캔은 HTTPS 및 `BarcodeDetector` 지원 환경에서 동작하며, 미지원 환경에서는 ID를 직접 입력할 수 있습니다.

## JSON 데이터

`schema_version`, `next_umbrella_id`, `umbrellas`, `events` 배열을 갖습니다. 이벤트는 추가 전용이며 `BORROW`, `RETURN`, `LOST`, `DAMAGED`, `DISPOSED`, `RESTORE`, `REGISTER`를 지원합니다. 가져오기 기능은 기존 브라우저 데이터를 교체합니다.
