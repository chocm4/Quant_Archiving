"""
Quantocracy 퀀트 라이브러리 수집기 v3
========================================
핵심 변경사항:
  - 전체 아카이브 수집: 226페이지 × 페이지당 ~10 Daily Wrap × Wrap당 3~5개 아티클
    → 최대 ~6,000~10,000개 아티클 수집 가능
  - Claude API 한국어 번역/요약 시스템 (배치 처리)
  - 중단/재개 가능한 체크포인트 시스템
  - KRX 점수 기반 자동 분류

RSS 구조 (실제 확인):
  https://quantocracy.com/author/quantadmin/feed/?paged=N
  - 총 226페이지 (2024년 4월 기준 page 20 = 2024-04)
  - 각 <item> = Daily Wrap (content:encoded 안에 .qo-entry 리스트)

설치:
    pip install feedparser requests beautifulsoup4 schedule anthropic

실행:
    # 기본: 최근 10페이지만 (빠른 시작)
    python quantocracy_scraper.py

    # 전체 아카이브 수집 (시간 소요: ~30분)
    python quantocracy_scraper.py --full-archive

    # 특정 페이지 범위
    python quantocracy_scraper.py --pages 1-50

    # 한국어 번역 (미번역 아티클 자동 번역)
    python quantocracy_scraper.py --translate
    python quantocracy_scraper.py --translate --limit 50  # 한 번에 50개

    # KRX 고점수 아티클 우선 번역
    python quantocracy_scraper.py --translate --krx-only

    # 통계 및 조회
    python quantocracy_scraper.py --stats
    python quantocracy_scraper.py --krx
    python quantocracy_scraper.py --export

    # 자동 스케줄 (매일 수집 + 번역)
    python quantocracy_scraper.py --schedule
"""

import sqlite3
import json
import time
import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import feedparser
    import requests
    from bs4 import BeautifulSoup
    import schedule
except ImportError:
    print("필수 패키지 설치: pip install feedparser requests beautifulsoup4 schedule")
    exit(1)

# ─── 설정 ────────────────────────────────────────────────────────────────────

DB_PATH     = Path("quant_library.db")
EXPORT_PATH = Path("quant_articles.json")

BASE_FEED   = "https://quantocracy.com/author/quantadmin/feed/"
MAX_PAGES   = 226          # 실제 확인된 총 페이지 수
HEADERS     = {"User-Agent": "QuantLibraryBot/3.0 (personal research archiver)"}
REQUEST_DELAY = 1.2        # 서버 부하 방지 (초)
TRANSLATE_BATCH = 10       # 번역 배치 크기

# ─── 태그 / KRX 점수 ─────────────────────────────────────────────────────────

TAG_RULES = {
    "momentum":  ["momentum","trend follow","52-week","moving average","breakout",
                  "cross-sectional momentum","relative strength","time-series momentum",
                  "trend following","trend-following"],
    "ml":        ["machine learning","deep learning","neural network","transformer",
                  "llm","gpt","random forest","xgboost","reinforcement learning",
                  "nlp","lstm","gradient boost","artificial intelligence","neural net",
                  "dspy","rag","embedding","autoencoder","hidden markov"],
    "factor":    ["factor","value stock","quality","profitability","anomaly","alpha",
                  "fama","french","smart beta","multifactor","low volatility",
                  "book-to-market","earnings yield","cross-section","factor model",
                  "factor invest","size premium"],
    "macro":     ["macro","interest rate","inflation","gdp","central bank","fed ",
                  "yield curve","currency","regime","business cycle","monetary policy",
                  "credit spread","economic","fomc","treasury","rate hike"],
    "portfolio": ["portfolio","allocation","diversification","risk parity","drawdown",
                  "sharpe","rebalancing","tactical","taa","mean-variance",
                  "hierarchical","kelly","leverage","return stack","portable alpha"],
    "options":   ["option","volatility surface","vix","implied vol","delta","gamma",
                  "derivatives","futures","skew","term structure","variance swap",
                  "vol regime","put-call","straddle","strangle"],
    "pairs":     ["pairs trading","cointegration","stat arb","statistical arbitrage",
                  "mean reversion","pairs","spread trading"],
    "crypto":    ["bitcoin","crypto","blockchain","defi","ethereum","digital asset",
                  "web3","altcoin"],
    "backtest":  ["backtest","out-of-sample","overfitting","walk-forward",
                  "data snooping","look-ahead","simulation","monte carlo"],
    "research":  ["paper","research","evidence","academic","study","empirical",
                  "journal","review","literature"],
}

KRX_SCORE_MAP = {
    "factor": 3, "momentum": 3, "value": 3, "cross-section": 3,
    "mean reversion": 3, "equity": 3, "stock selection": 3, "stock market": 3,
    "portfolio": 2, "allocation": 2, "regime": 2, "volatility": 2,
    "macro": 2, "earnings": 2, "fundamental": 2, "anomaly": 2,
    "risk parity": 2, "rebalancing": 2, "tactical": 2,
    "trend": 1, "machine learning": 1, "alpha": 1, "backtest": 1,
    "risk": 1, "diversification": 1, "sharpe": 1, "drawdown": 1,
    "bitcoin": -3, "crypto": -3, "defi": -3, "nft": -2,
    "forex": -1, "fx ": -1, "commodity": -1,
}

KRX_THRESHOLD = 3

# ─── DB ──────────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            title_ko     TEXT,
            url          TEXT NOT NULL,
            source       TEXT,
            published    TEXT,
            excerpt      TEXT,
            excerpt_ko   TEXT,
            summary_ko   TEXT,
            full_text    TEXT,
            tags         TEXT DEFAULT '[]',
            krx_flag     INTEGER DEFAULT 0,
            krx_score    INTEGER DEFAULT 0,
            krx_note_ko  TEXT,
            translated   INTEGER DEFAULT 0,
            bookmarked   INTEGER DEFAULT 0,
            rating       INTEGER DEFAULT 0,
            notes        TEXT,
            feed_page    INTEGER DEFAULT 0,
            feed_wrap    TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            updated_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS scrape_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scraped_at TEXT DEFAULT (datetime('now')),
            pages      TEXT,
            new_count  INTEGER DEFAULT 0,
            total      INTEGER DEFAULT 0,
            status     TEXT
        );

        CREATE TABLE IF NOT EXISTS scrape_checkpoint (
            id            INTEGER PRIMARY KEY CHECK (id=1),
            last_page     INTEGER DEFAULT 0,
            last_scraped  TEXT
        );

        CREATE TABLE IF NOT EXISTS krx_ideas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            period     TEXT,
            ideas_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_published  ON articles(published DESC);
        CREATE INDEX IF NOT EXISTS idx_krx_flag   ON articles(krx_flag);
        CREATE INDEX IF NOT EXISTS idx_krx_score  ON articles(krx_score DESC);
        CREATE INDEX IF NOT EXISTS idx_translated ON articles(translated);
        CREATE INDEX IF NOT EXISTS idx_bookmarked ON articles(bookmarked);
        CREATE INDEX IF NOT EXISTS idx_source     ON articles(source);
    """)
    conn.commit()

def make_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]

def auto_tag(title, excerpt):
    text = (title + " " + excerpt).lower()
    return [t for t, kws in TAG_RULES.items() if any(k in text for k in kws)]

def calc_krx_score(title, excerpt):
    text = (title + " " + excerpt).lower()
    return sum(v for k, v in KRX_SCORE_MAP.items() if k in text)

def extract_source(raw_title, url):
    m = re.search(r'\[([^\]]+)\]$', raw_title)
    if m:
        return m.group(1).strip(), raw_title[:m.start()].strip()
    try:
        domain = urlparse(url).netloc.replace("www.","").split(".")[0].title()
    except:
        domain = "Unknown"
    return domain, raw_title

# ─── RSS 파싱 ─────────────────────────────────────────────────────────────────

def parse_page(page_num):
    """
    한 페이지의 RSS를 파싱해 개별 아티클 리스트 반환.
    페이지당 Daily Wrap 10개, Wrap당 3~5개 아티클 → 약 30~50개/페이지
    """
    url = BASE_FEED if page_num == 1 else f"{BASE_FEED}?paged={page_num}"
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
    except Exception as e:
        print(f"  [페이지 {page_num} 오류] {e}")
        return [], False

    if not feed.entries:
        return [], False   # 페이지 범위 초과

    articles = []
    for item in feed.entries:
        wrap_title = item.get("title", "")
        pub = item.get("published_parsed")
        pub_str = datetime(*pub[:6]).strftime("%Y-%m-%d") if pub else ""

        content_html = ""
        if hasattr(item, "content") and item.content:
            content_html = item.content[0].get("value", "")
        elif hasattr(item, "summary"):
            content_html = item.summary

        if not content_html:
            continue

        soup = BeautifulSoup(content_html, "html.parser")
        for entry in soup.select(".qo-entry"):
            link_el = entry.select_one("a.qo-title")
            desc_el = entry.select_one(".qo-description")
            if not link_el:
                continue
            raw_title = link_el.get_text(strip=True)
            raw_url   = link_el.get("href", "")
            excerpt   = (desc_el.get_text(strip=True) if desc_el else "")[:800]
            source, title = extract_source(raw_title, raw_url)
            score = calc_krx_score(title, excerpt)
            articles.append({
                "id":        make_id(raw_url),
                "title":     title,
                "url":       raw_url,
                "source":    source,
                "published": pub_str,
                "excerpt":   excerpt,
                "tags":      auto_tag(title, excerpt),
                "krx_flag":  1 if score >= KRX_THRESHOLD else 0,
                "krx_score": score,
                "feed_page": page_num,
                "feed_wrap": wrap_title,
            })
    return articles, True

def save_articles(conn, articles, verbose=True):
    new_count = 0
    for a in articles:
        if conn.execute("SELECT 1 FROM articles WHERE id=?", (a["id"],)).fetchone():
            conn.execute("""
                UPDATE articles SET krx_score=?,krx_flag=?,tags=?,updated_at=datetime('now')
                WHERE id=?
            """, (a["krx_score"], a["krx_flag"],
                  json.dumps(a["tags"], ensure_ascii=False), a["id"]))
            continue
        conn.execute("""
            INSERT INTO articles
              (id,title,url,source,published,excerpt,tags,krx_flag,krx_score,feed_page,feed_wrap)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (a["id"],a["title"],a["url"],a["source"],a["published"],
              a["excerpt"],json.dumps(a["tags"],ensure_ascii=False),
              a["krx_flag"],a["krx_score"],a["feed_page"],a.get("feed_wrap","")))
        new_count += 1
        if verbose:
            flag = "KRX" if a["krx_flag"] else "   "
            print(f"  [{flag}] {a['published']} | {a['source']:<22} | {a['title'][:50]}")
    conn.commit()
    return new_count

# ─── 메인 수집 로직 ───────────────────────────────────────────────────────────

def scrape_pages(conn, start=1, end=10, verbose=True):
    """지정된 페이지 범위를 수집"""
    total_new = 0
    for page in range(start, end + 1):
        articles, has_content = parse_page(page)
        if not has_content:
            print(f"  페이지 {page}: 콘텐츠 없음 (범위 초과)")
            break
        new = save_articles(conn, articles, verbose=verbose)
        total_new += new
        total_in_page = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"  [페이지 {page:3d}/{MAX_PAGES}] Wrap {len(articles)//4:.0f}±개 파싱 → 신규 {new}개 (DB 누적: {total_in_page})")
        # 체크포인트 저장
        conn.execute("""
            INSERT INTO scrape_checkpoint(id,last_page,last_scraped) VALUES(1,?,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET last_page=excluded.last_page,last_scraped=excluded.last_scraped
        """, (page,))
        conn.commit()
        time.sleep(REQUEST_DELAY)
    return total_new

def get_checkpoint(conn):
    row = conn.execute("SELECT last_page FROM scrape_checkpoint WHERE id=1").fetchone()
    return row[0] if row else 0

# ─── 한국어 번역 시스템 ───────────────────────────────────────────────────────

def translate_articles(conn, limit=20, krx_only=False, verbose=True):
    """
    미번역 아티클을 Claude API로 한국어 번역/요약.
    배치 처리: 한 번 API 호출에 여러 아티클을 묶어서 효율 향상.
    """
    try:
        import anthropic
    except ImportError:
        print("anthropic 패키지 필요: pip install anthropic")
        return 0

    where = "translated=0"
    if krx_only:
        where += " AND krx_flag=1"
    rows = conn.execute(f"""
        SELECT id,title,excerpt,krx_score,krx_flag
        FROM articles WHERE {where}
        ORDER BY krx_score DESC, published DESC
        LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("번역할 아티클 없음 (모두 번역 완료)")
        return 0

    print(f"\n{'─'*60}")
    print(f"  번역 대상: {len(rows)}개 아티클 (배치 {TRANSLATE_BATCH}개씩)")
    print(f"{'─'*60}")

    client = anthropic.Anthropic()
    translated = 0

    # TRANSLATE_BATCH개씩 묶어서 처리
    for batch_start in range(0, len(rows), TRANSLATE_BATCH):
        batch = rows[batch_start:batch_start + TRANSLATE_BATCH]

        # 프롬프트 구성: 여러 아티클을 한 번에 번역 요청
        articles_input = "\n\n".join([
            f"[{i+1}] ID:{r[0]}\n제목: {r[1]}\n요약: {r[2][:400]}\nKRX점수:{r[3]}"
            for i, r in enumerate(batch)
        ])

        prompt = f"""당신은 퀀트 파이낸스 전문 번역가이자 한국 주식시장 전문가입니다.

아래 {len(batch)}개의 퀀트 파이낸스 아티클을 한국어로 번역/요약하고,
KRX 점수가 3 이상인 아티클은 한국 시장 적용 노트도 작성해주세요.

{articles_input}

각 아티클에 대해 정확히 다음 JSON 배열 형식으로만 출력하세요 (다른 텍스트 없이):
[
  {{
    "id": "원본 ID 그대로",
    "title_ko": "제목 한국어 번역",
    "excerpt_ko": "요약 한국어 번역 (2-3문장)",
    "summary_ko": "핵심 내용 한 줄 요약 (30자 이내)",
    "krx_note_ko": "KRX 점수 3 이상인 경우만: 한국 시장 적용 방법 1-2문장, 아니면 null"
  }}
]"""

        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r'^```json\s*', '', raw).strip()
            raw = re.sub(r'\s*```$', '', raw).strip()
            results = json.loads(raw)

            for r in results:
                conn.execute("""
                    UPDATE articles SET
                        title_ko=?,excerpt_ko=?,summary_ko=?,krx_note_ko=?,
                        translated=1,updated_at=datetime('now')
                    WHERE id=?
                """, (r.get("title_ko"), r.get("excerpt_ko"),
                      r.get("summary_ko"), r.get("krx_note_ko"), r["id"]))
                translated += 1
                if verbose:
                    print(f"  ✓ [{r['id']}] {r.get('title_ko','')[:50]}")
            conn.commit()
            print(f"  배치 {batch_start//TRANSLATE_BATCH+1} 완료: {len(results)}개 번역")

        except json.JSONDecodeError as e:
            print(f"  [번역 오류] JSON 파싱 실패: {e}")
            # 개별 처리로 폴백
            for row in batch:
                _translate_single(conn, client, row)
                translated += 1
        except Exception as e:
            print(f"  [API 오류] {e}")
            time.sleep(5)

        time.sleep(0.5)  # API 레이트 리밋 방지

    print(f"\n✓ 번역 완료: {translated}개")
    remaining = conn.execute("SELECT COUNT(*) FROM articles WHERE translated=0").fetchone()[0]
    print(f"  미번역 잔여: {remaining}개")
    return translated


def _translate_single(conn, client, row):
    """단일 아티클 번역 (폴백용)"""
    article_id, title, excerpt, krx_score, krx_flag = row
    prompt = f"""퀀트 파이낸스 아티클을 한국어로 번역해주세요.

제목: {title}
요약: {excerpt[:400]}
KRX점수: {krx_score}

다음 JSON 형식으로만 출력 (다른 텍스트 없이):
{{"title_ko":"","excerpt_ko":"","summary_ko":"30자이내핵심요약","krx_note_ko":{"null" if krx_score < 3 else '"KRX 적용 방법 1-2문장"'}}}"""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role":"user","content":prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r'^```json\s*','',raw).strip()
        raw = re.sub(r'\s*```$','',raw).strip()
        r = json.loads(raw)
        conn.execute("""
            UPDATE articles SET title_ko=?,excerpt_ko=?,summary_ko=?,krx_note_ko=?,
            translated=1,updated_at=datetime('now') WHERE id=?
        """, (r.get("title_ko"),r.get("excerpt_ko"),
              r.get("summary_ko"),r.get("krx_note_ko"),article_id))
        conn.commit()
    except Exception as e:
        print(f"    [단일 번역 오류] {article_id}: {e}")

# ─── KRX 아이디어 생성 ────────────────────────────────────────────────────────

def generate_krx_ideas(conn, days=30):
    try:
        import anthropic
    except ImportError:
        print("anthropic 패키지 필요")
        return

    rows = conn.execute("""
        SELECT title,title_ko,source,excerpt,tags,krx_score
        FROM articles
        WHERE krx_flag=1 AND published >= date('now',?)
        ORDER BY krx_score DESC, published DESC LIMIT 20
    """, (f"-{days} days",)).fetchall()

    if not rows:
        rows = conn.execute("""
            SELECT title,title_ko,source,excerpt,tags,krx_score
            FROM articles WHERE krx_flag=1
            ORDER BY krx_score DESC LIMIT 20
        """).fetchall()

    articles_text = "\n".join([
        f"- [{r[2]}] {r[1] or r[0]}\n  요약: {r[3][:200]}\n  태그: {r[4]} | 점수: {r[5]}"
        for r in rows
    ])

    client = anthropic.Anthropic()
    print(f"\n[Claude API] KRX 리포트 아이디어 생성 중 ({len(rows)}개 아티클 기반)...")

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        messages=[{"role":"user","content":f"""한국 주식시장(KRX/KOSPI/KOSDAQ) 전문 퀀트 리서처로서,
아래 글로벌 퀀트 연구들을 바탕으로 KRX 적용 가능한 리서치 아이디어 5개를 JSON으로만 출력하세요:

{articles_text}

[{{"title":"","hypothesis":"","source_articles":[],"data":[],"method":"","expected_edge":"","difficulty":"상|중|하","category":"팩터|모멘텀|ML|매크로|포트폴리오|이벤트드리븐"}}]"""}]
    )
    raw = re.sub(r'^```json\s*','',resp.content[0].text.strip())
    raw = re.sub(r'\s*```$','',raw).strip()
    try:
        ideas = json.loads(raw)
        period = datetime.now().strftime("%Y-%m")
        conn.execute("INSERT INTO krx_ideas(period,ideas_json) VALUES(?,?)",
                     (period, json.dumps(ideas,ensure_ascii=False)))
        conn.commit()
        for i, idea in enumerate(ideas, 1):
            print(f"\n  {i}. [{idea.get('category','')}] {idea['title']}")
            print(f"     {idea['hypothesis']}")
        print(f"\n✓ {len(ideas)}개 아이디어 저장")
        return ideas
    except Exception as e:
        print(f"[오류] {e}")

# ─── 내보내기 / 통계 ──────────────────────────────────────────────────────────

def export_json(conn, path=EXPORT_PATH):
    rows = conn.execute("""
        SELECT id,title,title_ko,url,source,published,excerpt,excerpt_ko,
               summary_ko,tags,krx_flag,krx_score,krx_note_ko,
               translated,bookmarked,rating,notes,created_at
        FROM articles ORDER BY published DESC
    """).fetchall()
    cols = ["id","title","title_ko","url","source","published","excerpt","excerpt_ko",
            "summary_ko","tags","krx_flag","krx_score","krx_note_ko",
            "translated","bookmarked","rating","notes","created_at"]
    data = []
    for row in rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        data.append(d)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"✓ {len(data)}개 아티클 → {path}")


def show_stats(conn):
    total      = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    krx        = conn.execute("SELECT COUNT(*) FROM articles WHERE krx_flag=1").fetchone()[0]
    translated = conn.execute("SELECT COUNT(*) FROM articles WHERE translated=1").fetchone()[0]
    bm         = conn.execute("SELECT COUNT(*) FROM articles WHERE bookmarked=1").fetchone()[0]
    chk        = get_checkpoint(conn)
    ideas_n    = conn.execute("SELECT COUNT(*) FROM krx_ideas").fetchone()[0]
    min_date   = conn.execute("SELECT MIN(published) FROM articles WHERE published!=''").fetchone()[0]
    max_date   = conn.execute("SELECT MAX(published) FROM articles WHERE published!=''").fetchone()[0]
    sources    = conn.execute("SELECT source,COUNT(*) c FROM articles GROUP BY source ORDER BY c DESC LIMIT 12").fetchall()
    tags_raw   = conn.execute("SELECT tags FROM articles").fetchall()
    tag_counts = {}
    for (t,) in tags_raw:
        for tag in json.loads(t or "[]"):
            tag_counts[tag] = tag_counts.get(tag,0)+1

    print(f"\n{'═'*55}")
    print(f"  Quantocracy 퀀트 라이브러리 통계")
    print(f"{'═'*55}")
    print(f"  총 아티클       {total:>6,}개")
    print(f"  수집 기간       {min_date} ~ {max_date}")
    print(f"  KRX 적용 가능   {krx:>6,}개  ({krx/total*100:.1f}%)")
    print(f"  한국어 번역 완료 {translated:>6,}개  ({translated/total*100:.1f}%)")
    print(f"  즐겨찾기        {bm:>6,}개")
    print(f"  마지막 수집 페이지  {chk}/{MAX_PAGES}")
    print(f"  KRX 아이디어 생성   {ideas_n}회")
    print(f"\n  상위 출처:")
    for src, cnt in sources[:10]:
        bar = "█" * min(cnt//2, 20)
        print(f"    {src:<25} {bar} {cnt}")
    print(f"\n  태그 분포:")
    for tag, cnt in sorted(tag_counts.items(),key=lambda x:-x[1])[:12]:
        bar = "█" * min(cnt//5, 20)
        print(f"    {tag:<14} {bar} {cnt}")
    print(f"{'═'*55}\n")


def show_krx(conn, limit=50):
    rows = conn.execute("""
        SELECT published,source,title,title_ko,tags,krx_score,translated
        FROM articles WHERE krx_flag=1
        ORDER BY krx_score DESC, published DESC LIMIT ?
    """, (limit,)).fetchall()
    print(f"\n KRX 적용 가능 아티클 TOP {limit} (점수순)")
    print(f"{'─'*70}")
    for pub,src,title,title_ko,tags,score,trans in rows:
        ko = "✓" if trans else " "
        print(f"  [{score:+3d}]{ko} {pub}  {src:<20}  {(title_ko or title)[:48]}")


# ─── 스케줄러 ─────────────────────────────────────────────────────────────────

def run_daily(conn):
    """매일 최신 페이지 1~2장만 수집 + 번역 10개"""
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] 일일 수집 시작")
    new = scrape_pages(conn, start=1, end=2, verbose=False)
    print(f"  신규: {new}개")
    if new > 0:
        translate_articles(conn, limit=new+5, verbose=False)


# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Quantocracy 퀀트 라이브러리 수집기 v3")
    p.add_argument("--full-archive", action="store_true", help=f"전체 아카이브 수집 (최대 {MAX_PAGES}페이지)")
    p.add_argument("--pages",        type=str, default="1-10", help="수집 페이지 범위 (예: 1-50, 기본 1-10)")
    p.add_argument("--translate",    action="store_true", help="미번역 아티클 한국어 번역")
    p.add_argument("--translate-all",action="store_true", help="전체 미번역 순차 번역")
    p.add_argument("--krx-only",     action="store_true", help="KRX 아티클만 번역")
    p.add_argument("--limit",        type=int, default=20,  help="번역 한 번에 처리할 최대 수 (기본 20)")
    p.add_argument("--ideas",        action="store_true", help="KRX 리포트 아이디어 생성")
    p.add_argument("--days",         type=int, default=30,  help="아이디어 생성 기준 기간")
    p.add_argument("--stats",        action="store_true", help="DB 통계")
    p.add_argument("--krx",          action="store_true", help="KRX 아티클 목록")
    p.add_argument("--export",       action="store_true", help="JSON 내보내기")
    p.add_argument("--schedule",     action="store_true", help="자동 스케줄 (매일 09:00/21:00)")
    p.add_argument("--resume",       action="store_true", help="중단된 수집 재개")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.schedule:
        print("스케줄러 시작 — 매일 09:00 / 21:00")
        schedule.every().day.at("09:00").do(run_daily, conn)
        schedule.every().day.at("21:00").do(run_daily, conn)
        while True:
            schedule.run_pending()
            time.sleep(60)

    elif args.full_archive:
        chk = get_checkpoint(conn) if args.resume else 0
        start = chk + 1 if chk > 0 else 1
        print(f"전체 아카이브 수집: 페이지 {start} ~ {MAX_PAGES}")
        print(f"예상 소요: 약 {(MAX_PAGES-start+1)*REQUEST_DELAY/60:.0f}분")
        total = scrape_pages(conn, start=start, end=MAX_PAGES)
        print(f"\n✓ 전체 수집 완료: {total}개 신규")
        show_stats(conn)

    elif args.resume:
        chk = get_checkpoint(conn)
        if chk == 0:
            print("체크포인트 없음. --pages 1-10 으로 시작하세요.")
        else:
            print(f"체크포인트 재개: 페이지 {chk+1}부터")
            total = scrape_pages(conn, start=chk+1, end=MAX_PAGES)
            print(f"\n✓ 재개 완료: {total}개 신규")

    elif args.translate or args.translate_all:
        limit = 99999 if args.translate_all else args.limit
        translate_articles(conn, limit=limit, krx_only=args.krx_only)

    elif args.ideas:
        generate_krx_ideas(conn, days=args.days)

    elif args.stats:
        show_stats(conn)

    elif args.krx:
        show_krx(conn)

    elif args.export:
        export_json(conn)

    else:
        # 기본: --pages 파라미터에 따라 수집
        try:
            if "-" in args.pages:
                s, e = map(int, args.pages.split("-"))
            else:
                s = e = int(args.pages)
        except:
            s, e = 1, 10
        print(f"페이지 {s}~{e} 수집 중...")
        total = scrape_pages(conn, start=s, end=e)
        print(f"\n✓ 완료: {total}개 신규")
        show_stats(conn)

    conn.close()

if __name__ == "__main__":
    main()
