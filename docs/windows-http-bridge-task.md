# Windows 자동 시작 (HTTP Bridge)

집 Windows 부팅 시 HTTP 브리지를 자동 시작합니다.

## 1회 설정 + 즉시 실행
PowerShell(관리자):

```powershell
cd \\wsl$\Ubuntu\home\san\band-ai-dashboard
powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows_native_bridge.ps1
```

## 사전 조건
- Python 설치
- 저장소 경로 예: `C:\work\band-ai-dashboard`
- `.worker.env`에 `BRIDGE_TOKEN` 설정 완료

## 등록
PowerShell(관리자):

```powershell
cd C:\work\band-ai-dashboard
powershell -ExecutionPolicy Bypass -File .\scripts\install_http_bridge_task.ps1 `
  -TaskName "BandAiCodexHttpBridge" `
  -RepoPath "C:\work\band-ai-dashboard" `
  -PythonExe "python" `
  -Port 8787
```

## 확인/수동 실행
```powershell
Get-ScheduledTask -TaskName "BandAiCodexHttpBridge"
Start-ScheduledTask -TaskName "BandAiCodexHttpBridge"
```

## Codex 설치 검증 (경로 혼선 방지)
- Windows PowerShell에서 확인:

```powershell
where codex
codex --version
npm root -g
dir "$((npm root -g))\@openai"
```

- WSL에서 확인(별도):

```bash
which codex
codex --version
npm root -g
ls "$(npm root -g)/@openai/codex/node_modules/@openai"
```

참고: Windows에서는 `codex-win32-x64`, WSL에서는 `codex-linux-x64`가 보여야 정상입니다.

## 삭제
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_http_bridge_task.ps1 `
  -TaskName "BandAiCodexHttpBridge"
```
