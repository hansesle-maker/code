# 모바일(iOS)에서 TSI 스캐너 돌리기 — 1부터 100까지

내가 켜둘 서버가 **하나도 없어도** 매 15분마다 자동으로 바이낸스 선물 전 종목을
스캔하고, iPhone에서 확인하는 방법입니다. 전부 무료입니다.

```
GitHub Actions (매 15분 cron, 무료 클라우드 실행기)
   └─ generate_static.py  ← 바이낸스 USDT-M 선물 전 종목 4h/1h/15m TSI 스캔
        ├─ index.html + data.json → GitHub Pages (항상 떠 있는 웹페이지)
        └─ 직전 스캔과 비교 → 변화한 종목만 Telegram 푸시 알림
```

- **웹페이지**: 전 종목 표를 매 15분 자동 갱신. iPhone에서 "홈 화면에 추가" → 앱처럼 사용.
- **Telegram**: 새로 3/3 강세/약세 정렬, 4h가 signal·0선을 돌파한 종목만 폰 알림으로 푸시.

---

## 준비물

- GitHub 계정 (이미 있음)
- (선택) Telegram 계정 — 푸시 알림을 원할 때만
- iPhone — 결과 확인용

---

## STEP 1~4 · 저장소를 공개(public)로 두기

> **왜?** GitHub Actions는 public 저장소에서 **무제한 무료**, GitHub Pages도
> public에서 무료입니다. private은 Actions 분당 한도(월 2000분)를 매 15분 실행이
> 금방 초과하고, Pages도 유료 플랜이 필요합니다. 시세는 공개 데이터이니 public 권장.

1. GitHub에서 `hansesle-maker/code` 저장소로 이동
2. **Settings** 탭 클릭
3. 맨 아래 **Danger Zone** → "Change repository visibility"
4. 이미 public이면 그대로 두면 됩니다

---

## STEP 5~7 · 코드 확인 (이미 푸시되어 있음)

이번 작업으로 아래 파일들이 `claude/friendly-sagan-azwrto` 브랜치에 들어있습니다.
`main`(또는 기본 브랜치)에 병합해야 Actions가 동작합니다.

| 파일 | 역할 |
|------|------|
| `.github/workflows/scan.yml` | 매 15분 실행 + Pages 배포 |
| `generate_static.py` | 스캔 → 정적 HTML/JSON 생성 → 텔레그램 알림 |
| `tsi_signal/scanner.py` | 전 종목 멀티 타임프레임 TSI 계산 |
| `tsi_signal/alerts.py` | 직전 결과와 비교 → 알림 메시지 |
| `templates/dashboard.html` | 모바일 대시보드 |

5. PR을 만들어 기본 브랜치로 병합 (또는 직접 머지)
6. **Settings → Actions → General → Workflow permissions**: "Read repository contents"
   기본값이면 OK (워크플로 안에서 Pages 권한을 따로 부여함)
7. 병합 완료

---

## STEP 8~12 · GitHub Pages 켜기

8. 저장소 **Settings → Pages**
9. **Source** 를 **"GitHub Actions"** 로 선택 (Deploy from a branch ❌)
10. 저장
11. 아직 사이트 주소는 안 떠도 정상 (첫 배포 후 생김)
12. 최종 주소는 이렇게 됩니다 → **`https://hansesle-maker.github.io/code/`**

---

## STEP 13~20 · (선택) Telegram 푸시 알림 설정

> 웹페이지만 쓸 거면 이 단계는 건너뛰어도 됩니다. 텔레그램 토큰이 없으면
> 스캔은 정상 동작하고 푸시만 자동으로 생략됩니다.

**봇 만들기**
13. 텔레그램에서 **@BotFather** 검색 → 대화 시작
14. `/newbot` 입력 → 봇 이름/사용자명 지정
15. BotFather가 주는 **토큰**을 복사 (예: `8123456789:AAE...xyz`)

**내 chat_id 알아내기**
16. 방금 만든 봇과 대화창을 열어 아무 메시지나 한 번 전송 (예: "hi")
17. 브라우저에서 열기: `https://api.telegram.org/bot<토큰>/getUpdates`
18. 응답 JSON에서 `"chat":{"id":123456789}` 의 숫자가 **chat_id**

**GitHub Secrets에 등록**
19. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
20. 두 개 등록:
    - `TELEGRAM_BOT_TOKEN` = 15번 토큰
    - `TELEGRAM_CHAT_ID` = 18번 숫자

---

## STEP 21~30 · 첫 실행 & 동작 확인 (가장 중요)

21. 저장소 **Actions** 탭 클릭
22. 왼쪽 목록에서 **"TSI Scan"** 워크플로 선택
23. 오른쪽 **"Run workflow"** 버튼 → **Run workflow** (수동 첫 실행)
24. `build` 잡이 도는 동안 로그 확인
25. ✅ `Scanning 300+ symbols × 3 timeframes …` 가 보이면 **바이낸스 접속 성공**
26. ❌ `ERROR: cannot reach Binance ... HTTP 451/403` 가 보이면
    → 아래 **"⚠️ Binance 지역 차단"** 섹션으로
27. `build` 성공 후 `deploy` 잡이 사이트를 배포
28. `deploy` 잡 로그 맨 위에 **page_url** (= `https://hansesle-maker.github.io/code/`)
29. 그 주소를 브라우저에서 열어 표가 뜨는지 확인
30. **첫 실행은 텔레그램 알림이 오지 않습니다** (비교할 직전 데이터가 없어 기준선만 저장).
    두 번째 실행부터 "변화한 종목"이 푸시됩니다.

---

## STEP 31~40 · iPhone에 앱처럼 설치

31. iPhone **Safari**로 `https://hansesle-maker.github.io/code/` 접속
32. 하단 **공유 버튼**(↑ 네모) 탭
33. **"홈 화면에 추가"** 선택
34. 이름 확인 후 **추가**
35. 홈 화면에 "TSI Scanner" 아이콘 생성 → 전체화면 앱처럼 실행
36. 페이지는 다음 15분봉 마감 후 **자동 새로고침** (상단에 "다음 갱신 m:ss" 카운트다운)
37. 텔레그램을 설정했다면, 알림은 잠금화면에 그대로 뜸
38. 알림 종류:
    - 🟢 3/3 강세 정렬 / 🔴 3/3 약세 정렬 (신규 전환)
    - ⚡ 4h TSI가 signal 상향 돌파 / 🔻 하향 이탈
    - 📈 4h TSI가 0선 상향 돌파 / 📉 하향 이탈
39. 끝! 이제 손 안 대도 매 15분마다 돌아갑니다
40. 비용: **0원** (public 저장소 기준)

---

## 화면 보는 법

각 종목 카드에 4h / 1h / 15m 한 줄씩:

| 표시 | 의미 |
|------|------|
| **TSI 값 색깔** | 🟢 초록 = 0보다 큼 / 🔴 빨강 = 0보다 작음 |
| **↑ / ↓** | 직전 마감 TSI 대비 상승 / 하락 (trend) |
| **>SIG / <SIG** | TSI가 signal 선보다 위 / 아래 |
| **카드 상단 점 3개** | 4h·1h·15m 순서. 🟢 완전강세 / 🔴 완전약세 / 🟡 혼합 |

- **필터 칩**: 전체 · 3/3 강세 · 2/3+ 강세 · 3/3 약세 · 4h 강세/약세 · 3TF 모두 ↑/↓
- **정렬**: A-Z · 강세순 · 약세순 · 4h/1h/15m TSI 값순
- **검색**: 심볼명 입력 (예: BTC)

---

## ⚠️ Binance 지역 차단 (451/403이 뜬 경우)

GitHub Actions 실행기는 대개 미국 리전이고, 바이낸스는 미국 IP의
`fapi.binance.com` 접근을 `451 (restricted location)`로 막습니다. STEP 25에서
이 에러가 나면 스캔 자체가 불가능합니다. 대안:

1. **한국 등 허용 지역에서 직접 실행 (가장 확실)**
   집/회사 PC에서 같은 스크립트를 cron(맥/리눅스) 또는 작업 스케줄러(윈도우)로
   15분마다 실행하고, 결과를 push 하거나 텔레그램만 받는 방법.
   ```bash
   pip install -r requirements.txt
   # 텔레그램만 받기 (웹페이지 배포 없이):
   export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
   python generate_static.py --out public --prev public/data.json
   ```
   PC가 꺼져 있으면 그 시간엔 안 돕니다. "서버 없이"와는 trade-off.

2. **허용 리전의 무료/저가 클라우드에 작은 cron 띄우기**
   Fly.io(도쿄 `nrt`/싱가포르 `sin` 리전), 오라클 클라우드 always-free VM 등
   바이낸스가 허용하는 리전에서 위 스크립트를 cron으로 실행.

3. **self-hosted runner**: 허용 지역의 내 머신을 GitHub Actions 러너로 등록.
   (사실상 1번과 동일하게 머신이 켜져 있어야 함)

> 참고: 한국에서 본인이 직접 접속하는 건 보통 문제없습니다. 차단은 **실행기 위치**의
> 문제이지 코드 문제가 아닙니다.

---

## 자주 묻는 것

- **cron이 정확히 15분마다 안 돌아요**
  GitHub 무료 cron은 부하가 몰리면 수 분 지연되거나 가끔 건너뜁니다. 정밀한 실시간이
  필요하면 위 1·2번(자체 cron)을 쓰세요.

- **한동안 알림이 안 와요**
  스케줄 워크플로는 **저장소가 60일간 활동이 없으면 자동 비활성화**됩니다.
  Actions 탭에서 다시 enable 하면 됩니다.

- **rate limit / IP 차단(418/429)**
  전 종목 × 3 타임프레임이라 요청이 많습니다. `--workers` 기본 8로 제한해 두었고,
  문제가 생기면 더 낮추거나 스캔 종목을 좁히세요(`--limit`).

- **PC에서 실시간으로 보고 싶어요 (라이브 서버)**
  ```bash
  pip install -r requirements.txt
  python web_scanner.py            # http://localhost:5000
  ```
  이건 켜져 있는 동안만 동작하는 옛 방식입니다. 평소엔 위의 GitHub Pages 방식 권장.
