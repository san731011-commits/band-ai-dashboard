# HTTP Bridge 운영 가이드

Discord 타임아웃을 피하기 위해 비동기 HTTP 방식으로 Codex 작업을 처리합니다.

## 핵심 방식
- `POST /jobs` 요청 시 즉시 `job_id` 반환 (`202 Accepted`)
- Codex 작업은 백그라운드에서 실행
- `GET /jobs/<job_id>`로 결과 조회

## 1) 설정
저장소 루트에 `.worker.env` 파일 생성:

```bash
cp .worker.env.example .worker.env
```

필수 항목:
```env
BRIDGE_TOKEN=long_random_token_here
CODEX_COMMAND=codex exec
CODEX_PROMPT_MODE=arg
CODEX_TIMEOUT_SEC=1800
```

선택 항목:
```env
BRIDGE_MAX_PROMPT_CHARS=12000
BRIDGE_MAX_OUTPUT_CHARS=30000
```

## 2) 서버 실행
```bash
python3 scripts/codex_http_bridge.py --repo-path . --host 0.0.0.0 --port 8787
```

## 3) API 호출 예시
1. 작업 생성
```bash
curl -sS -X POST "http://127.0.0.1:8787/jobs" \
  -H "Authorization: Bearer $BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"README 개선하고 커밋해줘"}'
```

응답 예시:
```json
{
  "job_id": "uuid",
  "status": "queued",
  "status_url": "/jobs/uuid",
  "created_at": "..."
}
```

2. 결과 조회
```bash
curl -sS "http://127.0.0.1:8787/jobs/<job_id>" \
  -H "Authorization: Bearer $BRIDGE_TOKEN"
```

3. 헬스 체크
```bash
curl -sS "http://127.0.0.1:8787/health"
```

## OpenClaw 연결 포인트
- Discord 명령 수신 후 `POST /jobs` 호출
- 즉시 `"접수됨 job_id=..."` 응답
- 백그라운드에서 `GET /jobs/<job_id>` 폴링
- `status=succeeded|failed` 되면 결과를 Discord로 회신
- 직접 연결 스크립트: `scripts/openclaw_http_bridge.py`
- Discord sidecar 봇: `scripts/discord_codex_bridge_bot.py`

## 보안 권장
- `BRIDGE_TOKEN`은 긴 랜덤값 사용
- 외부 인터넷 공개 금지(가능하면 Tailscale/사설망 내부만)
- 실패 로그/민감정보를 Discord에 원문 전송하지 않기
