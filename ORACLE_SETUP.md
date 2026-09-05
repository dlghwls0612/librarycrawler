# 오라클 클라우드(한국 무료서버) 자동수집 셋업 가이드

목표: **PC가 꺼져 있어도** 한국 IP 클라우드에서 매일 자동 수집 → GitHub에 결과 push → 사이트 자동 갱신.
비용: **영구 무료(Always Free)**. 소요: 처음 1회 약 30~40분. 막히면 화면/출력을 캡처해 물어보세요.

> ⚠️ 순서 중요: **먼저 4번 "성북 사전 점검"을 통과**해야 5번부터 진행해요. (오라클 IP도 막히면 헛수고 방지)

---

## 1. 오라클 클라우드 가입
1. https://www.oracle.com/kr/cloud/free/ → **무료로 시작하기**
2. 이메일·정보 입력, **홈 리전(Home Region)** = **South Korea Central (Chuncheon)** 또는 **South Korea North (Seoul)** ← 반드시 한국 리전!
3. 본인확인용 **신용/체크카드** 입력 (Always Free는 **과금 안 됨**, 확인용 1달러 임시승인만)
4. 가입 완료 → 콘솔 로그인

## 2. 우분투 VM(무료서버) 만들기
1. 콘솔 좌상단 ☰ → **Compute → Instances → Create instance**
2. Name: `librarycrawler`
3. **Image and shape → Edit**:
   - Image: **Canonical Ubuntu 22.04**
   - Shape: **Ampere (VM.Standard.A1.Flex)** → OCPU **1**, Memory **6 GB** (무료범위)
     - ※ "out of capacity" 뜨면 잠시 후 재시도하거나 Seoul↔Chuncheon 리전 변경
4. **Add SSH keys → Generate a key pair for me → Save private key**(파일 다운로드, 꼭 보관!)
5. **Networking**: "Assign a public IPv4 address" 켜져 있는지 확인
6. **Create** → 1~2분 후 상태 **Running**, **Public IP address** 메모 (예: 140.238.x.x)

## 3. SSH 접속 (Windows PowerShell)
다운로드한 키 파일 경로로 접속 (기본 사용자 `ubuntu`):
```powershell
ssh -i "C:\경로\ssh-key-....key" ubuntu@<공개IP>
```
- "The authenticity..." 물으면 `yes`
- 권한 오류 나면: 키 파일 우클릭 → 속성 → 보안에서 본인만 읽기 권한으로 (또는 그냥 진행되면 무시)

## 4. ★ 성북 사전 점검 (통과해야 다음 단계!) ★
접속된 우분투에서 그대로 붙여넣기:
```bash
curl -sk --max-time 15 "https://www.sbculture.or.kr/culture/bbs/BMSR00034/list.do?menuNo=500348&searchCnd=1&searchWrd=%EC%B1%84%EC%9A%A9" | grep -c "정규직"
```
- 결과가 **1 이상 숫자** → ✅ 오라클 한국 IP가 성북 통과! 5번으로.
- **0 또는 멈춤/timeout** → ❌ 오라클도 막힘. 여기서 멈추고 알려주세요(다른 방법 검토).

## 5. 크롤러 설치
```bash
sudo apt-get update && sudo apt-get install -y python3-pip python3-venv git
git clone https://github.com/dlghwls0612/librarycrawler.git ~/librarycrawler
cd ~/librarycrawler
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```
(마지막 줄은 몇 분 걸려요.)

## 6. GitHub 푸시 인증 (토큰)
서버가 결과를 GitHub에 올리려면 토큰이 필요해요.
1. GitHub → 우상단 프로필 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. Repository access: **Only select repositories → librarycrawler**
3. Permissions → **Contents: Read and write**
4. Generate → **토큰 문자열 복사**(한 번만 보임!)
5. 서버에서(토큰을 붙여넣어 실행):
```bash
cd ~/librarycrawler
git remote set-url origin https://<토큰>@github.com/dlghwls0612/librarycrawler.git
git config user.name "oracle-bot"
git config user.email "oracle-bot@users.noreply.github.com"
```

## 7. 실행 스크립트 만들고 첫 테스트
```bash
cat > ~/librarycrawler/run_and_push.sh <<'EOF'
#!/usr/bin/env bash
set -e
cd ~/librarycrawler
source .venv/bin/activate
git pull --rebase --autostash || true
python -m crawler.main --crawl
git add docs/data/jobs.json
git commit -m "auto: jobs.json 갱신 ($(date +%F))" || echo "변경 없음"
git push
EOF
chmod +x ~/librarycrawler/run_and_push.sh
bash ~/librarycrawler/run_and_push.sh
```
- 끝에 공고 수(예: "공고 35건")가 뜨고 push되면 성공! 사이트가 몇 분 뒤 갱신돼요.
- 성북이 목록에 들어왔는지 사이트에서 확인.

## 8. 매일 자동 실행 (cron)
```bash
( crontab -l 2>/dev/null; echo "0 20 * * * /bin/bash ~/librarycrawler/run_and_push.sh >> ~/crawl.log 2>&1" ) | crontab -
```
- 매일 **UTC 20:00 = 한국시간 새벽 5시** 자동 실행. 로그는 `~/crawl.log`.

## 9. GitHub Actions 스케줄 끄기 (충돌 방지)
이제 오라클이 매일 돌리니, GitHub Actions의 자동 스케줄은 꺼서 **미국 IP의 부분수집이 덮어쓰지 않게** 합니다.
- GitHub 웹에서 `.github/workflows/crawl.yml` 편집 → `schedule:` 두 줄(아래)만 삭제하고 `workflow_dispatch: {}`는 남김:
  ```
  schedule:
    - cron: "0 20 * * *"
  ```
- 커밋. (수동 Run은 계속 가능)

---

## 유지보수
- 서버 로그 보기: `tail -n 50 ~/crawl.log`
- 수동 즉시 실행: `bash ~/librarycrawler/run_and_push.sh`
- 크롤러 코드 고치면(내가 업데이트) 서버에서: `cd ~/librarycrawler && git pull` (run_and_push가 매번 pull하므로 자동 반영됨)
