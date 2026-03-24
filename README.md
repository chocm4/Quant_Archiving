# Quant Library — GitHub 설정 가이드

## 레포지토리 구조

```
your-repo/
├── .github/
│   └── workflows/
│       └── scrape.yml          ← 자동 수집 워크플로
├── docs/                       ← GitHub Pages 루트 (자동 생성)
│   ├── index.html              ← 웹 앱 (quant_library.html 복사본)
│   ├── quant_articles.json     ← 아티클 DB
│   └── krx_ideas.json          ← KRX 아이디어
├── quant_library.html          ← 웹 앱 소스
├── quantocracy_scraper.py      ← 스크래퍼
├── quant_library.db            ← SQLite DB (Actions에서 생성/유지)
└── README.md
```

---

## 1단계 — GitHub 레포지토리 생성

```bash
git init
git add .
git commit -m "init: Quant Library 초기 셋업"
git remote add origin https://github.com/YOUR_NAME/quant-library.git
git push -u origin main
```

---

## 2단계 — GitHub Pages 활성화

1. 레포지토리 → **Settings** → **Pages**
2. Source: **GitHub Actions** 선택
3. 저장

배포 후 주소: `https://YOUR_NAME.github.io/quant-library/`

---

## 3단계 — API Key 등록 (번역 자동화)

1. 레포지토리 → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret** 클릭
3. Name: `ANTHROPIC_API_KEY`
4. Value: Anthropic API 키 입력

> API 키 없이도 수집은 동작합니다. 번역 단계만 스킵됩니다.

---

## 4단계 — 첫 실행

### 자동 실행 확인
- 매일 한국 시간 오전 9시 자동 실행
- GitHub Actions 탭 → 워크플로 실행 확인

### 수동 실행
1. **Actions** 탭 → **Quantocracy 자동 수집 및 배포**
2. **Run workflow** 클릭
3. 옵션 설정:
   - `pages`: 수집 범위 (기본 `1-3`, 전체 아카이브는 `all`)
   - `translate`: 번역 여부 (기본 true)
   - `translate_limit`: 번역 최대 수 (기본 30)

### 전체 아카이브 최초 수집
```
pages 입력란에 "all" 입력 → Run workflow
```
약 30~60분 소요, 최대 ~10,000개 아티클 수집

---

## 5단계 — 로컬에서 웹 앱 열기

JSON 파일을 로컬에서 로드하려면 간단한 서버가 필요합니다
(브라우저 보안 정책상 `file://`에서 fetch 불가):

```bash
# Python 내장 서버
python -m http.server 8000

# 브라우저에서 열기
open http://localhost:8000/quant_library.html
```

또는 `quant_library.html`을 직접 열면 시드 데이터 20개로 동작합니다.

---

## 자동화 흐름 요약

```
매일 09:00 KST
    │
    ▼
GitHub Actions 실행
    │
    ├─ scrape.py --pages 1-3    (최신 약 30~50개 수집)
    ├─ translate --limit 30      (KRX 관련 우선 번역)
    ├─ ideas (월요일만)          (KRX 리포트 아이디어 재생성)
    ├─ export → quant_articles.json
    │
    ▼
git commit & push
    │
    ▼
GitHub Pages 자동 배포
    │
    ▼
브라우저 접속 → 항상 최신 데이터
```

---

## 비용 참고

| 항목 | 비용 |
|------|------|
| GitHub Actions | 무료 (월 2,000분, 일 1회 실행 시 충분) |
| GitHub Pages | 무료 |
| Anthropic API (번역) | 하루 30개 번역 ≈ $0.01~0.03 |
| Anthropic API (아이디어) | 주 1회 ≈ $0.05 |

월 총 API 비용: **약 $1 미만**

---

## 트러블슈팅

**Actions에서 `git push` 권한 오류**
→ Settings → Actions → General → "Read and write permissions" 체크

**Pages가 배포되지 않음**
→ Settings → Pages → Source를 "GitHub Actions"로 변경

**번역이 실행되지 않음**
→ Secrets에 `ANTHROPIC_API_KEY`가 등록되어 있는지 확인
