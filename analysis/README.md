# 알트코인 ↔ BTC 상관계수 · 베타 분석

특정 시점부터 현재까지 기간 동안, 각 알트코인이 BTC와 얼마나 함께 움직이는지(**상관계수**)와 얼마나 민감하게 반응하는지(**베타**)를 산출합니다. 두 가지 형태로 제공합니다.

| 도구 | 대상 | 실행 위치 |
|------|------|-----------|
| [`bithumb_btc_corr_beta.py`](bithumb_btc_corr_beta.py) | 빗썸 **원화마켓 전 종목** | 로컬 Python |
| [`../strategies/altcoin_btc_corr_beta.pine`](../strategies/altcoin_btc_corr_beta.pine) | 지정한 알트코인 최대 12개 | TradingView |

## 지표 정의

- **CORR** — 코인 수익률과 BTC 수익률의 피어슨 상관계수 (−1 ~ +1)
- **BETA** — BTC에 대한 민감도 = `cov(코인, BTC) / var(BTC)`. 베타 1.5면 평균적으로 BTC 수익률의 1.5배로 움직였다는 뜻 (주식의 지수 대비 베타와 동일 개념).
- **R²** — 상관계수의 제곱. 코인 변동성 중 BTC로 설명되는 비율.

수익률은 종가 대비 종가(close-to-close)의 단순 변화율이며, 베타 산출 시 BTC를 시장(벤치마크)으로 둡니다.

---

## 1. Python — 빗썸 원화마켓 전 종목

빗썸 공개 API(인증 불필요)로 원화마켓에 상장된 모든 코인의 캔들을 받아, 시작일부터 현재까지 BTC 대비 상관계수·베타를 계산해 상관계수 내림차순 표와 CSV로 출력합니다.

### 필요 조건
- Python 3.8+
- `requests` (이미 `requirements.txt`에 포함)

### 사용 예

```bash
# 2024-01-01(KST)부터 현재까지, 일봉 수익률 기준, 원화마켓 전 종목
python analysis/bithumb_btc_corr_beta.py --from 2024-01-01

# 2025-01-01부터, 1시간봉 기준, 상관계수 상위 30개만, CSV 저장
python analysis/bithumb_btc_corr_beta.py --from 2025-01-01 --interval 1h --top 30 --csv out.csv

# 특정 코인만
python analysis/bithumb_btc_corr_beta.py --from 2024-01-01 --symbols ETH,XRP,SOL

# 네트워크 없이 계산 로직만 검증
python analysis/bithumb_btc_corr_beta.py --self-test
```

### 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--from` | `2024-01-01` | 시작일 (KST). `YYYY-MM-DD` 또는 `YYYY-MM-DD HH:MM` |
| `--interval` | `24h` | 빗썸 캔들 간격: `1m 3m 5m 10m 30m 1h 6h 12h 24h` |
| `--benchmark` | `BTC` | 기준 코인 |
| `--symbols` | (전체) | 쉼표로 구분한 코인 목록으로 제한 |
| `--min-points` | `20` | 겹치는 수익률 관측치가 이보다 적은 코인은 제외 (신규 상장 코인 방어) |
| `--top` | `0` | 상관계수 상위 N개만 표시 (0 = 전체) |
| `--delay` | `0.05` | 요청 간 대기(초). 레이트리밋 배려 |
| `--csv` | — | 결과를 CSV로도 저장 |

### 출력 예

```
COIN          N     CORR     BETA     R^2          LAST(KRW)
-----------------------------------------------------------------
ETH         548    0.842    1.213    0.709      5,120,000
SOL         548    0.771    1.640    0.594        280,500
...
```

> 신규 상장 코인은 BTC와 겹치는 기간만으로 비교되며(`N`이 해당 코인의 관측치 수), 관측치가 `--min-points` 미만이면 자동 제외됩니다.

---

## 2. Pine — TradingView 지표

BTC **1D 또는 4H 차트**에 올리고, 설정에서 시작일(From)과 비교할 알트코인 심볼(최대 12개)을 지정하면 우측 상단 표에 상관계수·베타·R²가 **상관계수 내림차순**으로 표시됩니다.

- 계산은 차트 타임프레임 기준입니다. 일봉 베타를 보려면 1D, 스윙 베타를 보려면 4H 차트에 적용하세요.
- 시작일부터 현재 봉까지 **확장 윈도우**(expanding window)로 누적 계산합니다.
- 잘못되거나 비워둔 심볼은 자동으로 건너뜁니다(`ignore_invalid_symbol`).
- 히스토리 버퍼상 최대 2,900봉까지 누적합니다(일봉이면 약 8년).

> ⚠️ 투자 조언이 아닙니다. 상관·베타는 과거 데이터에 기반하며 시장 국면에 따라 변합니다.
