# TSI 롱/숏 신호 엔진 (Phase 1 — 신호 생성)

코인 워치리스트를 4시간 단위로 평가해서, 각 종목의 **목표(to-be) 포지션
(롱/숏/플랫)과 현재 대비 조정량**을 표로 뽑아줍니다. 지금까지 차트를 일일이
열어서 하던 판단을 자동화한 것이고, **주문은 사용자가 직접** 바이낸스에서
실행합니다(반자동). 의존성은 `requests` 하나뿐이라 어디서든 돕니다.

## 판단 로직 (2단계)

신호는 **게이트**와 **트리거**를 분리해서 만듭니다.

### 1) 게이트 — BTC 대비 상대강도 (방향 필터)
- 측정 **시작점(time)은 사용자가 수동 입력**합니다 (`ref_time`). 전저점/전고점은
  그 시작점의 *예시*일 뿐이고, 자동으로 찾지 않습니다.
- 시작점부터 현재까지 `종목/BTC` 상대 성과를 계산:
  `rs = (종목_now/종목_ref) / (BTC_now/BTC_ref) − 1`
  - `rs > 0` (BTC보다 강세) → **롱만** 후보 가능
  - `rs < 0` (BTC보다 약세) → **숏만** 후보 가능
- 게이트는 **방향만 허용**할 뿐, 그 자체로는 절대 진입하지 않습니다.
- `ref_time`이 비어 있으면 기본적으로 게이트를 건너뜁니다(`--require-ref`로 차단 가능).
- 벤치마크(BTC) 자신은 게이트를 건너뛰고 자체 TSI로만 판단합니다.

### 2) 트리거 — 해당 종목의 4h / 1h TSI (실제 진입 판단)
TSI를 1봉 기울기로 보지 않고 **두 기준선**으로 읽어 **4단계 상태**로 판단합니다.
- **0선**: TSI>0 강세권 / <0 약세권
- **시그널선**(TSI의 EMA): TSI>signal 상승 / <signal 하락

| TSI vs 0 | TSI vs signal | 상태 |
|---|---|---|
| >0 | >signal | **+2 확정 상승** |
| <0 | >signal | +1 반등 초기 |
| >0 | <signal | −1 되돌림 |
| <0 | <signal | **−2 확정 하락** |

- **롱(기본=확정형)**: 게이트 long + **4h 상태 +2** + **1h 시그널선 위**(상태 ≥ +1)
- **숏**: 게이트 short + **4h 상태 −2** + **1h 시그널선 아래**(상태 ≤ −1)
- 그 외 → **FLAT**. (예: 4h가 음수인데 시그널 아래면 올라도 진입 안 함 / 1h가 시그널과
  엇갈리면 대기)

**확신도 사이징**: `CONV = 4h상태 + 1h상태` 의 절댓값으로 사이즈를 차등합니다
(기본 `{4: 100%, 3: 60%, 2: 30%}`). 즉 4h·1h가 모두 강할수록 크게 잡습니다.

### 기본값 / 튜닝 (`tsi_signal/signals.py`의 `SignalParams`)
| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `tsi_long / tsi_short / tsi_signal` | 25 / 13 / 13 | TradingView 기본 TSI와 동일 |
| `require_zero_4h` | True | 4h가 0선까지 같은 편이어야 진입(확정형). False면 +1/−1도 허용(`--aggressive`) |
| `require_zero_1h` | False | True면 1h도 0선 조건까지 요구(더 엄격, `--require-zero-1h`) |
| `size_by_conviction` | {4:1.0, 3:0.6, 2:0.3} | 확신도(CONV)별 사이즈 비율 |
| `require_ref` | False | True면 `ref_time` 없는 종목은 신호 차단 (`--require-ref`) |

> **얼리 vs 확정**: 기본은 4h가 0선 위(+2)까지 확인된 뒤 진입이라 안전하지만 조금 늦습니다.
> 반등 초기를 빨리 잡고 싶으면 `--aggressive`(4h +1 허용). 어느 쪽이 나은지는 백테스트로
> 정하는 게 맞습니다.

## 사용법

```bash
# 1) 오프라인 데모 (네트워크 불필요 — 출력 형식과 로직 확인용)
python run.py --demo

# 2) 라이브 (기본값 = 바이낸스 USDⓈ-M 선물 klines)
python run.py --symbols config/symbols.csv --out signals.csv

# 3) 현물(spot) klines를 쓰고 싶으면
python run.py --symbols config/symbols.csv --market spot

# 테스트
python tests/test_signals.py      # 또는: pytest -q
```

### 워치리스트 CSV (엑셀 대체)
`config/symbols.example.csv` 참고. 컬럼:

| 컬럼 | 설명 |
|---|---|
| `symbol` | 바이낸스 심볼 (예: `BTCUSDT`) |
| `asset_class` | `crypto` (Phase 1) |
| `current_position` | 현재 포지션, 부호 포함: +롱 / −숏 / 0 플랫 (명목 단위) |
| `target_notional` | (선택) 종목별 목표 사이즈. 비우면 `--default-notional` |
| `ref_time` | **상대강도 측정 시작점**. ISO(`2026-06-01`, `2026-06-01 08:00`, `...T08:00:00Z`) 또는 epoch 초/ms. 비우면 게이트 생략 |

> **엑셀 주의:** `ref_time`에 **시:분**까지 넣을 때는 셀 맨 앞에 작은따옴표를 붙이세요 →
> `'2026-06-01 08:00`. 안 그러면 엑셀이 저장할 때 시간을 떼고 날짜만 남깁니다.
> 저장된 CSV에선 따옴표가 빠지고 엔진이 시:분까지 정확히 인식합니다. 시작점은 4시간봉
> 단위로 매칭되므로, 그 시점 이후 첫 4h봉이 기준이 됩니다.

> **시간대(KST 기본):** 시간만 적으면 **한국시간(KST, UTC+9)** 으로 해석합니다.
> UTC로 지정하려면 끝에 `Z` 또는 `+00:00`을 붙이세요 (예: `2026-06-01 08:00Z`).

> **데이터 마켓:** 기본은 **선물(USDⓈ-M)** klines(`fapi.binance.com/fapi/v1/klines`)라
> 선물 전용 심볼도 받아옵니다. 현물을 쓰려면 `--market spot`.

### 출력 표 보는 법
```
SYMBOL    RS%vsBTC  GATE   TSI4h   St4h  TSI1h   St1h  CONV  SIGNAL  SIZE%  CUR   TARGET  DELTA  ACTION
ETHUSDT   +7.04     long   +100.0  +2    +100.0  +2    +4    LONG    100    0     1000    +1000  ENTER_LONG
LINKUSDT  +7.04     long   +100.0  +2    -72.1   +1    +3    LONG    60     0     600     +600   ENTER_LONG
XRPUSDT   +7.04     long   +100.0  +2    +9.5    -1    +0    FLAT    0      0     0       +0     HOLD
```
- `GATE` = 상대강도가 허용하는 방향(long/short/both)
- `St4h` / `St1h` = 각 TF의 TSI 상태(+2/+1/−1/−2)
- `CONV` = `St4h + St1h` (확신도) → `SIZE%` 결정
- `SIGNAL` = 최종 판단(LONG/SHORT/FLAT), `SIZE%` = 목표 사이즈 비율
- `ACTION` = 거래소에서 할 일: `ENTER_*` 진입 / `SWITCH_*` 전환 / `EXIT` 청산 /
  `ADD`·`REDUCE` 증감 / `HOLD` 유지
- `DELTA` = `TARGET − CUR` (이만큼 조정)

위 예에서 **XRP**는 BTC 대비 강세(게이트 통과)·4h 확정 상승(+2)이지만 **1h TSI 값(+9.5)이
시그널선 아래(−1 상태)** 라 진입을 보류(FLAT)합니다 — "오르긴 하나 아직 시그널 위가 아님".

## 백테스트
전략을 과거 데이터로 검증·비교합니다. 라이브와 **동일한 결정 규칙**(`signals.decide`)을
봉마다 적용하므로, 백테스트한 그대로 실거래됩니다.

```bash
# 오프라인 데모 (합성 다중국면 데이터로 4개 변형 비교)
python backtest.py --demo

# 라이브 단일 설정 (로컬, 바이낸스 접근 필요)
python backtest.py --symbols config/symbols.csv

# 라이브: 4개 변형 비교 (Confirmed/Aggressive × 게이트 on/off) + 포트폴리오
python backtest.py --symbols config/symbols.csv --compare
```
옵션: `--aggressive`, `--no-gate`, `--fee-bps 5`, `--rs-lookback 30`, `--market spot`,
`--equity-csv eq.csv`.

지표: **RET**(총수익) · **CAGR**(연환산) · **SHARPE** · **MDD**(최대낙폭) · **B&H**(매수후보유)
· **EXP**(평균 노출) · **TRADES** · **WIN**(승률). → RET만 보지 말고 **Sharpe·MDD를 함께**.

방법론(룩어헤드 방지/현실성):
- t봉 종가 신호 → t→t+1 수익으로 **1봉 지연** 반영, **수수료**(turnover×bps) 차감.
- 1h 상태는 그 4h봉 안에서 마지막으로 마감된 1h봉 사용.
- 라이브의 수동 `ref_time` 게이트는 백테스트에선 **롤링 상대강도**(최근 N봉 BTC 대비)로 대체.
  `--no-gate`로 끄고 효과를 비교할 수 있습니다.

⚠️ 한계/주의:
- 이 샌드박스는 바이낸스가 막혀 라이브 백테스트는 **로컬에서** 실행.
- `--demo` 수치(Sharpe/CAGR)는 이상적 합성데이터라 **비현실적으로 높습니다** — 엔진 동작과
  변형 비교를 보는 용도이지 실거래 기대치가 아닙니다.
- **발견**: 시그널선 방식은 추세전환은 잘 잡지만, TSI가 +100에 포화되는 "완만하고 꾸준한
  상승"에는 덜 참여합니다(자주 −1로 빠짐). 추세 추종 비중을 높이려면 `--aggressive`, 또는 추후
  히스테리시스(진입은 확정 +2, 청산은 −2에서만) 도입을 검토.
- 펀딩비·슬리피지는 미반영(수수료만). 필요 시 추가 가능.

## 4시간 자동 실행 (cron)
바이낸스 4h 봉은 UTC 00·04·08·12·16·20시에 마감됩니다. 마감 직후 실행:
```cron
2 0/4 * * *  cd /path/to/repo && /usr/bin/python3 run.py --symbols config/symbols.csv --out signals.csv >> run.log 2>&1
```
엔진은 **마감된 봉만** 사용합니다(형성 중인 마지막 봉은 버림).

## 네트워크 제약 (중요)
- 이 클라우드 샌드박스는 egress allowlist 때문에 `api.binance.com` 접근이
  **차단**되어 있습니다(403). 그래서 라이브 fetch는 여기서 바로 안 됩니다.
- 해결: **(a)** 본인 PC/서버에서 실행(권장 — 어차피 4h마다 도는 로컬 작업), 또는
  **(b)** 이 환경의 egress 설정에 `api.binance.com`(선물은 `fapi.binance.com`) 추가.
- 시장 데이터 klines는 인증이 필요 없습니다(API 키 불필요). 키는 Phase 2(자동주문)에서만.

## 한계 / 다음 단계
- **TradingView 일치**: TSI 공식·기본값이 동일해서, 워밍업 봉이 충분하면(기본 1000봉)
  값이 사실상 일치합니다. 라이브 연결 후 한 종목으로 수치 대조해보길 권장.
- **Phase 2 (완전자동)**: 신호의 `delta`만큼 바이낸스 선물 포지션을 자동 조정
  (드라이런 → 한도/안전장치 → 실거래). API 키 필요.
- **Phase 3 (주식/ETF)**: 데이터·증권사 연동이 다르고, 상대강도 벤치마크도 BTC가
  아니라 지수(SPY/QQQ 등)로 바뀌어야 함.
- **백테스트**: `backtest.py` 제공(위 "백테스트" 참고). 펀딩비·워크포워드·파라미터
  스윕 등은 추가 여지.

## 프로젝트 구조
```
run.py                      # 신호 CLI 진입점 (cron이 4h마다 호출)
backtest.py                 # 백테스트 CLI (--demo / --symbols / --compare)
tsi_signal/
  indicators.py             # EMA, TSI, 상대강도 (순수 파이썬, 무의존성)
  data.py                   # 바이낸스 klines fetch + 합성 데이터 생성
  signals.py                # 게이트 + 트리거 + decide() (핵심 규칙/파라미터)
  engine.py                 # 워치리스트 로드 → 평가 → to-be 표/CSV
  backtest.py               # 봉별 백테스트(같은 decide 규칙) + 지표/비교
config/symbols.example.csv  # 워치리스트 양식
tests/test_signals.py       # 로직·백테스트 검증 (네트워크 불필요)
```
