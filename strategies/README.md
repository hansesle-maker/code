# BTC Confluence Master [6-in-1]

TradingView Pine Script v6 전략 — 백테스팅 성과가 좋은 6개 BTC 전략의 핵심 강점을 하나로 결합한 **합의(Confluence) 점수 기반 통합 전략**입니다.

파일: [`btc_confluence_master.pine`](btc_confluence_master.pine)

## 결합 방식

여섯 전략을 단순히 이어붙이는 대신, 각 전략에서 **가장 검증된 요소 하나씩**을 추출해 매 봉마다 상승/하락 투표를 하게 만들었습니다. 6표 중 기준 점수(기본 5표) 이상이 한 방향으로 모일 때만 진입합니다. 단일 지표의 휩쏘(whipsaw)를 나머지 지표들이 걸러주는 구조입니다.

| # | 원본 전략 | 가져온 요소 | 역할 |
|---|-----------|-------------|------|
| 1 | VWAP Trend Momentum | 세션 VWAP + EMA 기울기 레짐, 풀백 진입 타이밍, ATR 손절/익절, UT Bot 트레일링 스탑, 일일 최대 거래 수 제한 | 레짐 필터 + **리스크 관리 전체** |
| 2 | SuperTrend STRATEGY (KivancOzbilgic) | ATR 밴드 기반 추세 방향 (10, 3.0) | 추세 방향 투표 |
| 3 | Hull Suite Strategy (InSilico) | HMA-55 기울기 (`HULL > HULL[2]`) | 중기 추세 확인 투표 |
| 4 | Single EMA Cross | EMA 10/20 정렬 | 단기 모멘텀 투표 |
| 5 | Twin OTT (Anıl Özekşi) | VAR(VIDYA) 기반 OTT 밴드 상태 | 적응형 추세 추적 투표 |
| 6 | MACD ReLoaded (KivancOzbilgic) | VAR 기반 MACD 히스토그램 | 모멘텀 확인 투표 |

## 진입 (Entry)

- **Confluence 모드(기본)**: 합의 점수가 처음으로 기준(기본 5/6)에 도달하는 순간 진입 — SuperTrend/TOTT/MACD류 전략처럼 추세 초입을 포착.
- **VWAP Pullback 모드**: 점수가 기준 이상인 상태에서, 가격이 세션 VWAP까지 얕게 되돌린 뒤 추세 방향으로 재개될 때만 진입 — 전략 1의 로직으로, 횟수는 적지만 진입가가 유리.
- 기본 **Long Only** (BTC 현물 친화적). 숏 허용 토글 있음.
- 일일 최대 거래 수 제한(기본 6회)으로 과매매 방지.

## 청산 (Exit) — 4중 방어

1. **ATR 손절**: 진입가 − 2×ATR (하드 스탑)
2. **ATR 익절**: 진입가 + 3×ATR (토글 가능)
3. **UT Bot 트레일링 스탑**: 추세가 이어지면 이익을 따라가며 잠금
4. **합의 붕괴 청산**: 점수가 기준 이하(기본 2/6)로 떨어지면 조기 청산

손절과 트레일링 스탑 중 더 타이트한 쪽이 항상 적용됩니다.

## 사용법

1. TradingView 차트(BTCUSD/BTCUSDT)에서 Pine Editor를 열고 `btc_confluence_master.pine` 내용을 붙여넣기 → Add to chart.
2. Strategy Tester에서 성과 확인. 수수료 0.05%, 슬리피지 2틱이 기본 반영되어 있음.
3. 우측 상단 대시보드에서 6개 컴포넌트의 실시간 투표 상태와 점수를 확인.
4. 타임프레임 권장: 1H~4H (추세 컴포넌트들의 기본 파라미터가 스윙 성향). 15m 이하 사용 시 `Entry Confluence Score`를 6으로 올리거나 VWAP Pullback 모드 권장.

## 튜닝 포인트

- `Entry Confluence Score`: 낮추면 거래 수 증가·품질 하락, 높이면 그 반대.
- `Exit When Score Falls To`: 높이면 청산이 빨라져 손실 축소, 낮추면 추세를 길게 홀딩.
- `SL/TP Multiplier`: 기본 2.0/3.0 (손익비 1.5:1). 익절을 끄면 트레일링 스탑만으로 추세 전체를 추종.

> ⚠️ 투자 조언이 아닙니다. 백테스트 성과가 미래 수익을 보장하지 않으며, 실전 적용 전 충분한 기간의 백테스트와 소액 검증을 거치세요.
