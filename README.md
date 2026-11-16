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


## V6 서버별 기능 분리
슬래시 명령어 4개는 서버 `1558462262132351096`에서만 등록됩니다.
다른 서버에서는 멘션 하나만 보낸 경우 ID를 답장 형식의 일반 텍스트로 출력합니다.
봇 시작 시 기존 전역 슬래시 명령어를 제거하고 운영 서버 전용으로 등록합니다.
Discord에서 명령어 목록이 갱신되는 데 시간이 걸릴 수 있습니다.
`GUILD_ID` 환경변수는 더 이상 사용하지 않습니다.


## V7 송금 수신자 멘션
Railway 봇 서비스 Variables에 `DODO_BOT_ID`를 **도도봇 자체의 사용자 ID**로 추가하세요.
(도도봇 프로필 우클릭 → 사용자 ID 복사. 서버 ID/채널 ID가 아닙니다.)
설정하지 않으면 송금 알림 기능만 비활성화되며 기존 기능은 유지됩니다.

도도봇이 `송금 성공` 임베드를 올리고 그 본문에 `<@사용자ID>님에게` 형태로
수신자 멘션이 포함되어 있으면 같은 채널에 원본 결과를 참조해
`💰 송금 도착! @수신자` 메시지를 전송합니다.
송금 실패나 수신자 ID가 없는 메시지는 무시합니다.
실제 도도봇 임베드의 원본 멘션 형식이 다르면 추가 수정이 필요할 수 있습니다.
`MESSAGE CONTENT INTENT`를 활성화하고 해당 채널에서 메시지 보기/보내기 권한을 주세요.

V8: transfer notification supports edited embeds and logs unmatched recipient mentions. Set DODO_BOT_ID=1047234279802949703.

V9: 송금 성공 메시지의 수신자와 송금 금액을 표시합니다. 송금 수수료는 제외합니다.


## V10 ID 출력 채널 제한
운영 서버 `1558462262132351096`에서는 `1566775552076156948`, `1566774361736872138` 두 채널에서만 단독 유저 멘션에 ID로 답장합니다. 다른 서버에서는 기존처럼 모든 접근 가능한 채널에서 ID 출력이 가능합니다. 송금 수신자 알림과 나눔/선착순 명령어는 이 제한의 영향을 받지 않습니다.

V11: Owner-server transfers are copied to the configured log channel; notifications reply to the copy. Transfers already in the log channel are not copied. Other guilds keep original behavior.


## V12 변경 사항
송금 성공 감지, 송금 내역 전달, 수신자 멘션 알림은 운영 서버(1558462262132351096)에서만 작동합니다. 다른 서버에서는 멘션 단독 메시지에 대한 ID 답장만 사용 가능합니다.

V15: 선착순 모집 정원 도달 또는 시간 만료 시 최종 명단을 새 메시지로 게시합니다.

V16: First-come closure announcements use Discord embeds, with ordered participant roster and pagination.


## V17 숙제 완료 자동 인증
운영 서버의 도도봇 땅파기 성공 결과에서 오늘도수고박스 지급 문구와 명령 실행자를 확인하면 인증 채널(1566775227885555742)에 날짜/멘션/박스 수량/실제 유저 프로필 썸네일을 담은 임베드를 올립니다. 멘션 알림은 보내지 않습니다. PostgreSQL로 KST 날짜별 중복 게시를 방지합니다. 원본 링크는 포함하지 않습니다. 명령 실행자 메타데이터가 없는 경우에는 안전하게 건너뛰고 로그를 남깁니다. 기존 DODO_BOT_ID 환경변수가 필요합니다.


## V18: 숙제 인증 웹후크
- 인증 채널 `1566775227885555742`에서만 숙제 완료 인증을 웹후크로 전송합니다.
- 메시지 작성자 이름과 프로필은 완료한 유저의 서버 닉네임과 아바타를 사용합니다.
- 임베드 오른쪽 썸네일은 제거했습니다.
- 제목은 한국 날짜, 본문은 알림 없는 멘션과 실제 수고박스 개수를 표시합니다.
- 인증 채널에서 봇에 **웹후크 관리(Manage Webhooks)** 권한이 필요합니다.
- 동일한 `도도 숙제 자동인증` 웹후크를 재사용합니다.
- 도도봇의 실제 메시지에서 실행자 정보를 제공하지 않으면 인증하지 않습니다.
