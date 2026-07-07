# 알트코인 ↔ BTC 상관계수 · 베타 분석

특정 시점부터 현재까지 기간 동안, 각 알트코인이 BTC와 얼마나 함께 움직이는지(**상관계수**)와 얼마나 민감하게 반응하는지(**베타**)를 산출합니다. 두 가지 형태로 제공합니다.

| 도구 | 대상 | 실행 위치 |
|------|------|-----------|
| [`bithumb_btc_corr_beta.py`](bithumb_btc_corr_beta.py) | 빗썸 **원화마켓 전 종목** | 로컬 Python |
| [`binance_futures_btc_corr_beta.py`](binance_futures_btc_corr_beta.py) | 바이낸스 **USDT-M 선물 전 종목** | 로컬 Python |
| [`../strategies/altcoin_btc_corr_beta.pine`](../strategies/altcoin_btc_corr_beta.pine) | 지정한 알트코인 최대 12개 | TradingView |

> 두 Python 스크립트는 상관·베타 계산 로직을 [`corr_beta.py`](corr_beta.py) 공통 모듈로 공유합니다. 거래소 API 호출부만 다릅니다.

## 웹 UI (5000포트)

명령줄 대신 브라우저에서 돌리려면 저장소 루트의 [`../web_corr_beta.py`](../web_corr_beta.py):

```bash
python web_corr_beta.py            # http://0.0.0.0:5000
```

거래소(빗썸/바이낸스 선물), 시작·종료 시각(KST, `datetime-local` 입력), 인터벌, 최소 관측치, 상위 N, 종목 제한을 폼에서 고르면 백그라운드로 전 종목을 스캔하며 진행률 바를 보여주고, 상관계수 내림차순 정렬 표(헤더 클릭 시 컬럼별 재정렬)와 CSV 저장을 제공합니다.

> Oracle Cloud VM에서 열려면 방화벽·보안목록에서 포트를 열어야 합니다(스크립트 실행 시 정확한 명령이 출력됩니다):
> ```bash
> sudo firewall-cmd --permanent --add-port=5000/tcp && sudo firewall-cmd --reload
> ```
> 그리고 VCN Security List(또는 NSG)에 TCP 5000 Ingress 규칙을 추가하세요.

## Pulse Entry 스크리너 (`pulse_entry.py` / 웹 탭 `/pulse`)

`Pulse Entry Engine [trade_w_samet]` 인디케이터의 신호 로직을 파이썬으로 포팅해, **Binance USDT-M 선물 전 종목**의 최근 **종료된** 봉(기본 15m)에서 LONG/SHORT 진입 신호를 실시간 스크리닝합니다. 웹앱 상단 "⚡ Pulse Entry 스크리너" 링크(`/pulse`).

- 코어 오실레이터는 **Martin Pring의 Special K**(고정 12-요소 가중합; 원본 Pine의 724봉 워밍업과 일치). 시그널선은 SpecialK의 SMA(길이 100).
- 거리/스트레치, Zero·EMA 리버설 필터, 0–100 리버설 점수, 모드 프리셋(Balanced/Aggressive/Scalping/Swing/Funded), 신호 상태머신, TP/SL 트레이드 차단 게이트까지 **원본 로직 그대로** 포팅.
- 모드·인터벌·자산군·최소 점수 선택, READY 표시 토글, 60초 자동 refresh 지원. 백그라운드 스캔 + 진행률 바.

> ⚠️ **정확도 주의**: `specialK` 코어는 Pring 표준 공식과 724봉 워밍업으로 검증했지만, TradingView `ta.specialK`의 **시그널선 스무딩(length2)** 내부가 다르면 신호가 차트와 미세하게 어긋날 수 있습니다. 정확히 일치시키려면 `ta` 라이브러리 소스를 주시면 `_signal_line`을 맞추겠습니다. 또 스크리너는 HTF 필터 OFF(원본 기본값), Bar Close 확정 기준입니다.

## TradingView 스크립트 랭킹 (`tv_scripts.py` / 웹 탭)

트레이딩뷰 공개 스크립트 목록(`/scripts/`)을 받아 **부스트 많은순 / 최신순 / 제목순**으로 정렬해 봅니다. 웹앱 상단 "📜 TV 스크립트 랭킹" 링크(`/tv`) 또는 CLI:

```bash
python analysis/tv_scripts.py --pages 3 --sort boosts
python analysis/tv_scripts.py --pages 5 --sort date --csv scripts.csv
python analysis/tv_scripts.py --dump 1 > page1.html   # 파서 교정용 원본 HTML
```

> ⚠️ 트레이딩뷰 ToS상 자동 수집은 제한될 수 있어 **개인용·소량·저빈도**로만 쓰세요. 또 사이트가 Cloudflare 봇 차단 + 클라이언트 렌더링이라, 단순 요청으로 카드가 안 잡히면 결과가 0개로 나올 수 있습니다. 그럴 땐 `--dump 1`(또는 웹의 `raw: page1 확인` 링크)로 실제 응답을 확인해 파서를 맞추면 됩니다.

## 지표 정의

- **CORR** — 코인 수익률과 BTC 수익률의 피어슨 상관계수 (−1 ~ +1)
- **BETA** — BTC에 대한 민감도 = `cov(코인, BTC) / var(BTC)`. 베타 1.5면 평균적으로 BTC 수익률의 1.5배로 움직였다는 뜻 (주식의 지수 대비 베타와 동일 개념).
- **R²** — 상관계수의 제곱. 코인 변동성 중 BTC로 설명되는 비율.

수익률은 종가 대비 종가(close-to-close)의 단순 변화율이며, 베타 산출 시 BTC를 시장(벤치마크)으로 둡니다.

### 기간 지정 (`--from` / `--to`)

두 Python 스크립트 모두 **시작·종료 시각을 분/초 단위까지** 지정할 수 있습니다.

- 날짜만: `--from 2024-01-01`
- 날짜+시간: `--from 2026-07-04T06:00` (명령줄에서 공백이 인자를 쪼개므로 **`T` 구분자 권장**. 공백을 쓰려면 `--from "2026-07-04 06:00"`처럼 따옴표)
- 종료 지정: `--to 2026-07-04T18:00` (생략 시 현재까지)
- **시간대**: 오프셋을 안 붙이면 **KST(UTC+9)** 로 해석. UTC 차트 시각을 그대로 쓰려면 오프셋을 붙입니다 — `--from 2026-07-04T06:00Z` 또는 `+00:00`, 명시적 KST는 `+09:00`.

> 예: 바이낸스 UTC 차트에서 본 "07-04 06:00"을 그대로 넣으려면 `--from 2026-07-04T06:00Z`. 빗썸(원화)은 KST가 자연스러우니 오프셋 없이 `--from 2026-07-04T06:00`.

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

## 2. Python — 바이낸스 USDT-M 선물 전 종목

바이낸스 선물 공개 API(인증 불필요)로 USDT-M **무기한(PERPETUAL) 전 종목**의 klines를 받아, 시작일부터 현재까지 BTCUSDT 대비 상관계수·베타를 계산합니다. 사용법·옵션은 빗썸 스크립트와 동일하며 인터벌 표기만 바이낸스 방식입니다.

### 사용 예

```bash
# 2024-01-01(KST)부터 현재까지, 일봉 기준, USDT-M 무기한 전 종목
python analysis/binance_futures_btc_corr_beta.py --from 2024-01-01

# 4시간봉, 상관계수 상위 30개, CSV 저장
python analysis/binance_futures_btc_corr_beta.py --from 2025-01-01 --interval 4h --top 30 --csv out.csv

# 특정 종목만
python analysis/binance_futures_btc_corr_beta.py --from 2024-01-01 --symbols ETHUSDT,SOLUSDT
```

### 옵션 (빗썸과 차이나는 부분)

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--interval` | `1d` | 바이낸스 klines 간격: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M` |
| `--benchmark` | `BTCUSDT` | 기준 심볼 |
| `--quote` | `USDT` | 스캔할 견적 자산 (예: `USDC`로 바꾸면 USDC-M) |
| `--asset-class` | `all` | `crypto` / `tradfi` / `all`. `tradfi`는 주식·ETF·상품(SK하이닉스·SOXL·XAU 등) 무기한만 |

> **TradFi(주식/ETF/상품) 무기한 포함:** 바이낸스는 이들을 `contractType = TRADIFI_PERPETUAL`, `underlyingType = KR_EQUITY / US_EQUITY / COMMODITY` 등으로 상장합니다. 스크립트는 `PERPETUAL`로 끝나는 모든 계약을 잡으므로 크립토와 TradFi가 함께 조회됩니다. TradFi만 보려면 `--asset-class tradfi`:
> ```bash
> python analysis/binance_futures_btc_corr_beta.py --from 2026-01-01 --asset-class tradfi
> ```

나머지(`--from --symbols --min-points --top --delay --csv --self-test`)는 빗썸 스크립트와 동일합니다. klines는 1회 최대 1,500개라 장기간·짧은 인터벌은 자동 페이지네이션합니다.

#### 상장 종목 전수 확인 (`--list-universe`)

이 스크립트는 크립토만 걸러내는 필터가 없습니다 — 필터는 `PERPETUAL + 지정 quote + TRADING`뿐이라 **바이낸스 선물이 상장한 심볼이면 종류와 무관하게 전부** 대상입니다. 바이낸스가 주식/ETF/지수 등 비(非)크립토 선물을 상장했다면 이미 결과에 포함됩니다.

실제로 어떤 종류가 있는지 눈으로 확인하려면:

```bash
python analysis/binance_futures_btc_corr_beta.py --list-universe
```

각 심볼의 `underlyingType`(예: `COIN`, `INDEX`)과 `underlyingSubType`를 출력하고 종류별 개수를 요약합니다. 비크립토 상품이 있으면 여기서 바로 드러나고, 그 심볼을 `--symbols`로 넣어 BTC 대비 상관·베타를 낼 수 있습니다.

---

## 3. Pine — TradingView 지표

BTC **1D 또는 4H 차트**에 올리고, 설정에서 시작일(From)과 비교할 알트코인 심볼(최대 12개)을 지정하면 우측 상단 표에 상관계수·베타·R²가 **상관계수 내림차순**으로 표시됩니다.

- 계산은 차트 타임프레임 기준입니다. 일봉 베타를 보려면 1D, 스윙 베타를 보려면 4H 차트에 적용하세요.
- 시작일부터 현재 봉까지 **확장 윈도우**(expanding window)로 누적 계산합니다.
- 잘못되거나 비워둔 심볼은 자동으로 건너뜁니다(`ignore_invalid_symbol`).
- 히스토리 버퍼상 최대 2,900봉까지 누적합니다(일봉이면 약 8년).

> ⚠️ 투자 조언이 아닙니다. 상관·베타는 과거 데이터에 기반하며 시장 국면에 따라 변합니다.
