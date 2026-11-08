# 도도 편의봇 V1

기능: 무료 나눔 / 선착순 참여 / 단독 유저 멘션 → ID 코드블록 출력.

## 디스코드 개발자 포털
1. Applications → New Application → Bot → Reset Token에서 토큰 복사.
2. Bot → Privileged Gateway Intents → **MESSAGE CONTENT INTENT** 켜기.
3. OAuth2 → URL Generator: `bot`, `applications.commands` 선택.
4. Bot Permissions: View Channels, Send Messages, Read Message History, Embed Links 권한 선택. 메시지 삭제 권한 불필요.
5. 초대 URL로 서버에 봇 초대.

## Railway 배포
1. 새 GitHub 저장소에 `bot.py`, `requirements.txt`, `Procfile` 업로드.
2. Railway에서 GitHub 저장소로 새 서비스 배포.
3. Railway 프로젝트에 PostgreSQL 데이터베이스 추가.
4. 봇 서비스 Variables에 `DISCORD_TOKEN`과 `DATABASE_URL` 설정. DATABASE_URL은 Railway PostgreSQL이 제공하는 연결 변수 참조(`${{Postgres.DATABASE_URL}}` 등 실제 서비스 이름에 맞게 설정).
5. 봇 서비스 Start Command는 `python bot.py`로 설정 (Procfile worker도 제공).
6. 테스트 서버에서 즉시 슬래시 명령어를 쓰려면 `GUILD_ID`에 서버 ID 설정. 비워두면 글로벌 명령어로 등록되어 반영까지 시간이 걸릴 수 있음.

## 명령어
- `/나눔생성 상품:<내용> 당첨인원:<수> 마감분:<분>`
- `/선착순생성 내용:<내용> 모집인원:<수> 마감분:<분>`
- `/이벤트취소 이벤트번호:<번호>` — 주최자만 진행 중 이벤트 취소
- `/나눔재추첨 이벤트번호:<번호>` — 주최자만 종료된 나눔 재추첨 (기존 당첨자도 다시 뽑힐 수 있음)
- 일반 채팅에 **정확히 한 명의 유저 멘션만** 전송 → 해당 유저 ID를 코드블록으로 답장. 원본 멘션은 유지.

## 운영 규칙
- 나눔 참여자는 누구나 '참여자 명단' 버튼으로 조회 가능. 나눔 종료 시 무작위 추첨.
- 선착순은 정원이 차면 즉시 마감, 대기자 없음. 정원 도달 전에는 취소 가능. 정원 도달 후 취소/추가 참여 불가.
- 정원 미달 시 지정한 마감 시각에 자동 종료.
- DB에 저장된 이벤트는 Railway 재시작 후에도 유지.
- 이 봇은 도도 공식 재화 송금/지급을 하지 않음.
- 나눔 추첨 메시지 전송 직전에 Railway 프로세스가 중단되는 극히 드문 경우 당첨자는 DB에 남지만 발표 메시지가 누락될 수 있음. 원본 이벤트 카드에서 당첨자 확인 가능.
- 봇이 읽을 수 있는 채널의 멘션에만 반응. Message Content Intent가 필수.
