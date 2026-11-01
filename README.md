# ShuffleBoost Discord 코드 알림 봇

Python 3.12~3.13, `discord.py` 2.6.1 이상 기반의 Windows용 봇입니다. 공개 Telegram 미리보기 `https://t.me/s/shuffleboost`를 기본 10초 간격으로 확인하고, 새 보너스 코드를 SQLite에 기록한 뒤 Discord Components V2 패널로 게시합니다.

## Discord 봇 준비

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 **New Application**을 만들고 **Bot** 메뉴에서 봇을 추가합니다.
2. Bot 설정의 **Privileged Gateway Intents**에서 **Message Content Intent**를 켭니다. 접두사 명령어(`!도움말` 등)를 읽을 때 필요합니다. 이 봇은 서버 멤버 인텐트는 요구하지 않습니다.
3. OAuth2의 **URL Generator**에서 `bot` 범위를 선택하고 다음 권한을 지정합니다.
   - 채널 보기 (View Channels)
   - 메시지 보내기 (Send Messages)
   - 메시지 기록 보기 (Read Message History; 불확실한 재전송 결과의 nonce 중복 확인에 필요)
   - 임베드 링크 (Embed Links)
   - 역할 관리 (Manage Roles; `!알림역할 생성`과 역할 토글에 필요)
   - 역할 멘션 허용: 코드 알림 역할의 **Allow anyone to @mention this role** 설정을 켭니다. 봇 메시지는 설정된 알림 역할만 멘션하도록 제한됩니다.
4. 생성된 URL로 봇을 서버에 초대합니다. 봇의 역할은 알림 역할보다 위에 있어야 역할을 부여/제거할 수 있습니다.

## 설치 및 실행

Python 3.12 또는 3.13을 설치한 다음 PowerShell에서 이 폴더로 이동합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

`.env`에서 `DISCORD_TOKEN`을 실제 봇 토큰으로 바꾸고, `ADMIN_IDS`에 관리자 Discord 사용자 ID를 쉼표로 구분해 입력합니다. 사용자 ID는 Discord 설정의 **고급 > 개발자 모드**를 켠 뒤 사용자 메뉴의 **사용자 ID 복사**로 확인할 수 있습니다. 토큰을 코드나 공개 저장소에 넣지 마세요.

GitHub에 소스를 올릴 때는 `.env` 대신 `.env.example`만 포함하세요. `.gitignore`가 실제 환경 설정, 가상환경, 로그와 SQLite 데이터베이스를 제외합니다. 봇 토큰이나 실제 관리자 ID를 `.env.example`에 넣지 마세요.

```powershell
python bot.py
```

시작 즉시 최신 글 페이지를 먼저 확인해 기준점을 설정하고, 과거 `BACKFILL_MAX_PAGES` 페이지는 백그라운드에서 기록합니다. 기존 게시물은 드랍하지 않습니다. 기본 10초마다 최신 페이지를 확인하며, 마지막 처리 번호보다 최신 글이 여러 페이지에 걸쳐 쌓였으면 `POLL_CATCHUP_MAX_PAGES` 한도 내에서 오래된 글부터 따라잡습니다. 본문에 코드가 없는 최근 5개 글은 `shufflecommunity.com` 링크를 Chromium 브라우저로 열어 Telegram 게시 날짜와 일치하는 날짜형 코드를 확인합니다. 브라우저는 코드 확인 시 표시되며, 보안 확인이 나타나면 직접 완료할 수 있습니다. 페이지가 열리지 않거나 구조가 바뀌면 코드를 추측하지 않고 지연 후 재시도합니다. 조회는 HTTPS 링크만 허용하고 페이지 본문은 1MB로 제한합니다. HTTP 429 응답은 `Retry-After`를 따릅니다. Discord에 봇이 온라인으로 표시된 후 관리자 계정으로 `!채널설정`을 실행하면 현재 채널이 드랍 채널로 지정됩니다.

## 설정값

- `DISCORD_TOKEN`: Discord Bot 토큰 (필수)
- `ADMIN_IDS`: 추가 관리자 사용자 ID 목록 (쉼표 구분, 서버 관리자 권한도 인정)
- `TG_CHANNEL`: 감시할 공개 Telegram 채널명 (`@` 없이, 기본 `shuffleboost`)
- `POLL_INTERVAL_SECONDS`: 조회 간격 초 (기본 10, 최소 10)
- `BACKFILL_MAX_PAGES`: 시작 시 백필 페이지 수 (기본 10)
- `POLL_CATCHUP_MAX_PAGES`: 중단 후 새 글 따라잡기 최대 페이지 수 (기본 20)
- `DATABASE_PATH`: SQLite 파일 경로 (기본 `shufflebot.sqlite3`)
- `STREAM_STATUS_TEXT`: Discord 스트리밍 상태 문구 (기본 `> yubin12 코드로 방송중 <`)
- `STREAM_STATUS_URL`: 스트리밍 상태를 눌렀을 때 열 주소 (기본 `https://www.twitch.tv/yubin12`)

## 명령어

- `!도움말`: 명령어 안내
- `!채널설정`: 현재 채널을 코드 드랍 채널로 지정
- `!알림역할 @역할` 또는 `!알림역할 생성`: 알림 역할 지정/생성
- `!패널`: 고정 패널 하나를 최신 저장 코드로 갱신 (패널이 없을 때만 새로 생성)
- `!기록 [개수]`: 지난 코드 기록 및 페이지 이동 버튼 (기본 10, 최대 50)
- `!기록동기화 [페이지수]`: 과거 게시물을 드랍 없이 기록에 백필
- `!상태`: 조회 시각, 마지막 글 번호, 코드 수, 실패/일시정지 상태 표시
- `!일시정지` / `!재개`: 코드 드랍을 일시정지/재개
- `!테스트`: 테스트 임베드 전송 (기록에는 저장하지 않음)

자동 드랍은 최초 한 번만 패널 메시지를 만들고, 이후에는 그 메시지를 수정해 코드 하나만 유지합니다. `!패널`도 같은 고정 메시지를 최신 저장 코드로 갱신합니다. 패널의 코드 복사/지난 코드/내 알림 설정 버튼은 명령어와 별개로 모든 사용자가 쓸 수 있습니다. 사용자가 켠 DM 알림은 사용자별 최소 보너스 필터를 적용합니다.

## 파서 테스트

```powershell
python -m unittest -v test_parser.py
```

로그는 콘솔과 `bot.log`에 기록됩니다. 설정과 코드 기록은 `shufflebot.sqlite3`에 저장됩니다.