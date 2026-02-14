# Worker Automation (1번)

집 Windows에서 GitHub 이슈를 자동 처리하는 워커입니다.

## 준비
1. 저장소 루트에서 설정 파일 생성
```bash
cp .worker.env.example .worker.env
```
2. `.worker.env`에 `GITHUB_TOKEN` 입력
3. Codex 실행 방식 확인
- 기본: `CODEX_COMMAND=codex exec`, `CODEX_PROMPT_MODE=arg`

## 수동 1회 실행 테스트
```bash
python3 scripts/github_task_worker.py --repo-path . --issue 1 --dry-run
```

`dry-run`이 성공하면 실제 실행:
```bash
python3 scripts/github_task_worker.py --repo-path . --issue 1
```

## 상시 폴링 실행
```bash
python3 scripts/github_task_worker.py --repo-path . --loop --interval 45
```

## 처리 규칙
- 대상: `task` 라벨 + `open` 상태 이슈
- 제외: `in-progress`, `done`, `canceled` 라벨
- 성공 시:
  - 코드 변경이 있으면 브랜치 푸시 + PR 생성
  - 이슈에 PR 링크 코멘트
- 실패 시:
  - `failed` 라벨 추가
  - 실패 코멘트 작성
