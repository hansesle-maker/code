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
- **롱**: 4h TSI 방향이 **상승** AND 1h TSI ≥ 0
- **숏**: 4h TSI 방향이 **하락** AND 1h TSI ≤ 0
- 게이트가 그 방향을 허용하고 **동시에** 트리거가 켜질 때만 포지션을 잡습니다.
  아니면 **FLAT**.

> 예) BTC보다 강세(게이트=long)이고, 4h TSI가 상승 전환 + 1h TSI가 0 이상 → **롱**.
> 반대면 숏.

### 기본값 / 튜닝 (`tsi_signal/signals.py`의 `SignalParams`)
| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `tsi_long / tsi_short / tsi_signal` | 25 / 13 / 13 | TradingView 기본 TSI와 동일 |
| `slope_lookback` | 1 | 4h TSI "방향"을 몇 봉 기울기로 볼지 |
| `require_reversal` | False | True면 4h TSI가 이번 봉에 *전환*(하락→상승)해야 함 (`--require-reversal`) |
| `one_h_threshold` | 0.0 | 롱은 1h TSI ≥ +t, 숏은 ≤ −t (버퍼를 주고 싶을 때) |
| `require_ref` | False | True면 `ref_time` 없는 종목은 신호 차단 (`--require-ref`) |

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

> **데이터 마켓:** 기본은 **선물(USDⓈ-M)** klines(`fapi.binance.com/fapi/v1/klines`)라
> 선물 전용 심볼도 받아옵니다. 현물을 쓰려면 `--market spot`.

### 출력 표 보는 법
```
SYMBOL  RS%vsBTC  GATE   TSI4h 4hDir  TSI1h  SIGNAL  CUR   TARGET  DELTA  ACTION
ETHUSDT  +4.85    long   +47.4 up     +47.4  LONG    0     1000    +1000  ENTER_LONG
```
- `GATE` = 상대강도가 허용하는 방향(long/short/both)
- `SIGNAL` = 최종 판단(LONG/SHORT/FLAT)
- `ACTION` = 거래소에서 할 일: `ENTER_*` 진입 / `SWITCH_*` 전환 / `EXIT` 청산 /
  `ADD`·`REDUCE` 증감 / `HOLD` 유지
- `DELTA` = `TARGET − CUR` (이만큼 조정)

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
- **백테스트**: 과거 데이터로 이 규칙의 성과를 검증/튜닝 (지표 로직이 순수 함수라
  그대로 재사용 가능).

## 프로젝트 구조
```
run.py                      # CLI 진입점 (cron이 4h마다 호출)
tsi_signal/
  indicators.py             # EMA, TSI, 상대강도 (순수 파이썬, 무의존성)
  data.py                   # 바이낸스 klines fetch + 합성 데이터 생성
  signals.py                # 게이트 + 트리거 → LONG/SHORT/FLAT (핵심 로직/파라미터)
  engine.py                 # 워치리스트 로드 → 평가 → to-be 표/CSV
config/symbols.example.csv  # 워치리스트 양식
tests/test_signals.py       # 로직 검증 (네트워크 불필요)
```
