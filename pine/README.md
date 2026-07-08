# Smart Money Concepts Strategy [LuxAlgo] — 전략 변환

LuxAlgo의 오픈소스 지표 **Smart Money Concepts [LuxAlgo]** (CC BY-NC-SA 4.0)를
트레이딩뷰 **전략(strategy)** 으로 변환한 스크립트입니다.

- 파일: `smart_money_concepts_strategy.pine` (Pine Script v5)
- 원본 지표의 모든 탐지/표시 로직(내부·스윙 구조, BOS/CHoCH, 오더블록, EQH/EQL,
  FVG, 전일/주/월 고저, Premium/Discount 존)은 그대로 유지되며,
  그 위에 진입·청산 실행 레이어가 추가되었습니다.
- 원본과 달리 **구조 탐지는 표시 설정과 무관하게 항상 실행**되므로, 라벨 표시를
  꺼도 전략 시그널은 정상 동작합니다. 트레일링 스윙 극값(Strong/Weak High/Low)도
  손절·익절·존 필터 계산을 위해 항상 갱신됩니다.

## 전략 로직 (지표 설명서의 Usage 섹션 반영)

### 진입
| 설정 | 옵션 | 설명 |
|---|---|---|
| Trade Direction | Long & Short / Long Only / Short Only | 허용 매매 방향 |
| Entry Structure | Internal / Swing / Both | 진입에 사용할 구조. Internal은 잦은 신호, Swing은 큰 구조 돌파 |
| Entry Signal | All / BOS / CHoCH | BOS=추세 지속 돌파, CHoCH=추세 전환 돌파 |
| Swing Trend Filter | on/off | 내부 구조 진입을 스윙 추세 방향과 일치할 때만 허용 |
| Premium/Discount Filter | on/off | 롱은 Equilibrium(50%) 아래(Discount 측), 숏은 위(Premium 측)에서만 진입 — 설명서의 "Discount 매수 / Premium 매도" 개념 |
| Start Date | 날짜 | 백테스트 시작일 |

### 청산
| 설정 | 옵션 | 설명 |
|---|---|---|
| Close on Opposite Break | on/off | 반대 방향 구조 돌파 시 포지션 종료(또는 리버설) |
| Stop Loss | Trailing Swing Points / ATR / Percent | Trailing Swing Points는 최근 Strong/Weak High·Low를 무효화 레벨로 사용 — 설명서의 "Strong High를 손절로" 개념 |
| ATR Multiplier | 숫자 | ATR(200) 배수 손절 거리. 다른 방식이 유효하지 않을 때의 폴백 기준으로도 사용 |
| Stop % | 숫자 | 퍼센트 손절 거리 |
| Take Profit | Risk/Reward / Equilibrium / Opposite Extreme / None | Equilibrium은 트레일링 레인지 50% 레벨 목표 — 설명서의 "Equilibrium을 익절로" 개념. Opposite Extreme은 반대편 Strong/Weak 극값 목표 |
| Risk/Reward Ratio | 숫자 | 손절 거리 대비 보상 배수. 선택한 목표가 유효하지 않으면 폴백으로 사용 |

선택한 손절/익절 레벨이 유효하지 않은 경우(예: 롱 진입인데 손절 레벨이 진입가
위) 자동으로 ATR 손절 / R:R 익절로 대체되어 주문 오류를 방지합니다.

### 기본 전략 속성
- 초기 자본 10,000 / 주문 크기: 자본의 100%
- 수수료 0.05% / `process_orders_on_close = true` (신호 봉 종가 체결)

## 사용 방법
1. 트레이딩뷰 차트에서 Pine 에디터를 열고 파일 내용을 붙여넣은 뒤 "차트에 추가"
2. 전략 테스터 탭에서 백테스트 결과 확인
3. `Strategy` 입력 그룹에서 진입 구조·필터·손익절 방식을 조합해 최적화

원본 지표의 알림(alertcondition) 16종은 그대로 유지되어 있으며, 전략 주문
알림은 트레이딩뷰의 주문 체결 알림 기능으로 받을 수 있습니다.

## 라이선스
원본과 동일하게 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) —
© LuxAlgo, 비상업적 사용 및 동일조건변경허락.
