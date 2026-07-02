# BTC Confluence Master [MTF 6-in-1]

TradingView Pine Script v6 전략 — 백테스팅 성과가 좋은 6개 BTC 전략을, **각 전략이 실제로 잘 통하는 시간 프레임에 맞춰** 결합한 멀티 타임프레임(MTF) 전략입니다.

파일: [`btc_confluence_master.pine`](btc_confluence_master.pine)

## 왜 멀티 타임프레임인가

6개 전략은 성격이 다릅니다:

- **VWAP Trend Momentum**은 세션 VWAP을 매일 리셋하는 **인트라데이(1H) 전용** 로직입니다. 4H에서는 하루 6봉밖에 안 되어 VWAP 풀백이 거의 작동하지 않습니다.
- **SuperTrend / Hull-55 / EMA / TOTT / MACD**는 파라미터가 **4H 스윙 추세**에 맞춰져 있습니다.

따라서 6개를 단일 프레임에서 동시에 투표시키는 것은 부적합합니다. 대신 **역할을 시간 프레임별로 분리**했습니다.

## 구조

```
   4H (상위 프레임)  →  방향(Bias): 어느 쪽으로 매매할 것인가
   ─────────────────────────────────────────────────────────
     SuperTrend · Hull-55 · EMA 10/20 · TOTT · MACD-VAR
     → 5개가 4H에서 투표하여 0~5 상승/하락 점수 산출 (request.security, 리페인트 없음)

   1H (차트 프레임)  →  타이밍 + 리스크: 언제 들어가고 어떻게 관리할 것인가
   ─────────────────────────────────────────────────────────
     VWAP Trend Momentum
     → 세션 VWAP 풀백 진입, ATR 손익절, UT Bot 트레일링, 일일 거래 제한
```

**핵심 규칙**: 4H 편향 점수가 기준(기본 4/5) 이상으로 한 방향에 모일 때만, 1H에서 VWAP 풀백 진입을 무장(arm)합니다. 4H 스윙 스택이 방향을 정하고, 1H VWAP 엔진이 진입 순간과 트레이드 관리를 담당합니다.

| # | 원본 전략 | 프레임 | 가져온 요소 |
|---|-----------|--------|-------------|
| 1 | VWAP Trend Momentum | **1H** | 세션 VWAP 레짐 + 풀백 진입, ATR 손익절, UT Bot 트레일링, 일일 최대 거래 수 |
| 2 | SuperTrend (KivancOzbilgic) | **4H** | ATR 밴드 추세 방향 (10, 3.0) |
| 3 | Hull Suite (InSilico) | **4H** | HMA-55 기울기 |
| 4 | Single EMA Cross | **4H** | EMA 10/20 정렬 |
| 5 | Twin OTT (Anıl Özekşi) | **4H** | VAR(VIDYA) OTT 밴드 상태 |
| 6 | MACD ReLoaded (KivancOzbilgic) | **4H** | VAR 기반 MACD 히스토그램 |

## 진입 (Entry)

4H 편향이 상승(≥4/5)이고 아래 트리거가 발생하면 진입합니다(숏은 반대):

- **VWAP Pullback 모드(기본)**: 1H에서 가격이 세션 VWAP까지 얕게 되돌린 뒤 추세 방향으로 재개될 때.
- **VWAP Cross 모드**: 1H 종가가 세션 VWAP을 편향 방향으로 돌파할 때.
- `Require 1H VWAP regime to agree`(기본 ON): 1H의 VWAP 위/아래 + EMA 기울기까지 4H 편향과 일치해야 진입.
- 기본 **Long Only**. 숏 허용 토글 있음.
- 일일 최대 거래 수 제한(기본 6회).

## 청산 (Exit) — 4중 방어

1. **ATR 손절**: 진입가 ∓ 2×ATR (1H 하드 스탑)
2. **ATR 익절**: 진입가 ± 3×ATR (토글 가능, 손익비 1.5:1)
3. **UT Bot 트레일링 스탑**: 추세가 이어지면 이익 잠금 (손절과 비교해 더 타이트한 쪽 적용)
4. **4H 편향 반전 청산**: 상위 프레임 편향이 반대로 뒤집히면 조기 청산

## 사용법

1. TradingView에서 **BTC 1시간 차트**를 엽니다 (BTCUSD/BTCUSDT).
2. Pine Editor에 `btc_confluence_master.pine`을 붙여넣고 Add to chart.
3. 설정에서 **Higher Timeframe = 240(4H)** 를 유지합니다 (기본값).
4. Strategy Tester에서 성과 확인. 수수료 0.05%, 슬리피지 2틱이 기본 반영됨.
5. 우측 상단 대시보드에서 4H 5개 컴포넌트의 투표 상태 + 1H VWAP 레짐 + 편향 점수를 실시간 확인.

> 다른 프레임 조합도 가능합니다. 예를 들어 15m 차트 + HTF 1H, 또는 4H 차트 + HTF 1D 로 스케일을 올릴 수 있습니다. 다만 VWAP 엔진은 인트라데이(1D 미만) 차트에서 의미가 있으므로 차트 프레임은 1H 안팎을 권장합니다.

## 튜닝 포인트

- `HTF Bias Score` (1~5): 낮추면 거래 증가·품질 하락, 높이면 그 반대. 5로 두면 4H 5개 전부 합의해야 진입(가장 보수적).
- `Exit When 4H Bias Flips`: 끄면 ATR/트레일링만으로 관리하여 추세를 더 오래 홀딩.
- `SL/TP Multiplier`: 기본 2.0/3.0. 익절을 끄면 트레일링 스탑만으로 추세 전체 추종.
- 리페인트 방지: 4H 신호는 `request.security(..., [1], lookahead_off)` 로 **직전에 확정된 4H 봉** 값만 사용합니다. 살짝 지연되지만 미래 참조가 없어 백테스트=실거래가 일치합니다.

> ⚠️ 투자 조언이 아닙니다. 백테스트 성과가 미래 수익을 보장하지 않으며, 실전 적용 전 충분한 기간의 백테스트와 소액 검증을 거치세요.
