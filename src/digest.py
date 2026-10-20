#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zheshe-daily-digest
每日从「哲社预印本」(zsyyb.cn) 抓取最新论文，按研究兴趣筛选推荐，
并附当日新论文清单，邮件推送到邮箱。

数据源：哲社预印本平台（中国人民大学，基于 ChinaXiv）
- 无 AI 依赖，无需任何大模型密钥
- 每天北京时间 06:20 推送（GitHub Actions cron `20 22 * * *`，UTC）
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORDS_FILE = os.path.join(BASE, "config", "keywords.json")
SUBJECTS_FILE = os.path.join(BASE, "config", "subjects.json")
STATE_FILE = os.path.join(BASE, "data", "sent_ids.json")

HOME_URL = "https://zsyyb.cn/"
SEARCH_URL = "https://zsyyb.cn/user/search.htm"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# 北京时间时区
CN_TZ = timezone(timedelta(hours=8))

# ---- 环境变量 ----
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SENDER = os.environ.get("SENDER", "")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "")
RECEIVER = os.environ.get("RECEIVER", "")

TOP_N = int(os.environ.get("TOP_N", "10"))          # 推荐篇数
LIST_MAX = int(os.environ.get("LIST_MAX", "100"))   # 清单上限

FOCUS = "应用经济学 / 工商管理学 / 管理科学与工程 · 企业创新 / 数字化转型 / 人工智能应用 / 贝叶斯与机器学习"


def cn_today():
    return dt.datetime.now(CN_TZ).date()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    try:
        return load_json(STATE_FILE)
    except Exception:
        return {"sent_ids": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _solve_challenge(session, url, resp_text):
    """应对平台 WAF 的 md5(IP) JS 挑战：解出 token 并重定向放行。

    高频请求会被平台要求 JS 验证，挑战页形如
    `window.location.href="?xxx="+md5("<客户端IP>")`，IP 明文写在响应里。
    算出 md5(IP) 作为 token 带上重定向即可通过。返回放行后的页面文本；
    若非挑战页返回 None。
    """
    m_ip = re.search(r'md5\("([\d.]+)"\)', resp_text)
    m_key = re.search(r'[\?&]([a-zA-Z0-9]+)="\+md5', resp_text)
    if not m_ip or not m_key:
        return None
    token = hashlib.md5(m_ip.group(1).encode("utf-8")).hexdigest()
    sep = "&" if "?" in url else "?"
    r = session.get(f"{url}{sep}{m_key.group(1)}={token}", timeout=40)
    if r.status_code == 200 and r.text:
        r.encoding = "utf-8"
        return r.text
    return None


def _get(session, url, **kwargs):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=40, **kwargs)
            if r.status_code == 200 and r.text:
                r.encoding = "utf-8"
                # WAF md5(IP) 挑战：响应含 md5 挑战脚本 → 解挑战放行
                if 'md5("' in r.text and "window.location.href" in r.text:
                    solved = _solve_challenge(session, url, r.text)
                    if solved and 'md5("' not in solved:
                        return solved
                return r.text
            print(f"[warn] HTTP {r.status_code} len={len(r.text)} (第{attempt + 1}次) {url}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[warn] 请求失败（第{attempt + 1}次）：{e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def build_session():
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": SEARCH_URL,
    })
    # 先访问首页建立 JSESSIONID（详情页/翻页需要带 cookie 才能访问）
    _get(sess, HOME_URL)
    return sess


# ---------------------------------------------------------------------------
def parse_list(html_text):
    """解析搜索列表页，返回论文列表。"""
    soup = BeautifulSoup(html_text, "lxml")
    papers = []
    for li in soup.select("div.list ul li"):
        pid = ""
        em = li.select_one("em a")
        if em:
            m = re.search(r"PSSXiv:([\d.]+)", em.get_text(strip=True))
            if m:
                pid = m.group(1)
        if not pid:
            h3a = li.select_one("h3 a")
            if h3a and h3a.get("href"):
                pid = h3a["href"].rsplit("/", 1)[-1]
        if not pid:
            continue

        # PDF uuid（通过 downloadFile('uuid',...) 的 onclick 传递）
        pdf_uuid = ""
        m = re.search(r"downloadFile\('([0-9a-f]{32})'", str(li))
        if m:
            pdf_uuid = m.group(1)
        pdf_url = f"https://zsyyb.cn/user/download.htm?uuid={pdf_uuid}" if pdf_uuid else ""

        h3 = li.select_one("h3 a")
        title = h3.get_text(strip=True) if h3 else ""

        authors = [a.get_text(strip=True) for a in li.select("div.name a")]

        cats, date = [], ""
        for span in li.select("p span"):
            label = span.select_one("font.label")
            if not label:
                continue
            lt = label.get_text(strip=True)
            if "分类" in lt:
                for a in span.find_all("a"):
                    t = a.get_text(strip=True)
                    if t:
                        cats.append(t)
            elif "时间" in lt:
                date = span.get_text(" ", strip=True).replace(lt, "").strip()

        abstract = ""
        for p in li.find_all("p"):
            f = p.select_one("font.label")
            if f and "摘要" in f.get_text(strip=True):
                abstract = p.get_text(" ", strip=True).replace(f.get_text(strip=True), "").strip()
                break

        papers.append({
            "id": pid,
            "title": title,
            "authors": authors,
            "category": cats,
            "date": date,
            "abstract": abstract,
            "url": f"https://zsyyb.cn/abs/{pid}",
            "doi": f"10.12451/{pid}",
            "pdf_url": pdf_url,
        })
    return papers


def fetch_papers(subject_ids):
    """按学科 ID 抓取各学科最新论文，合并去重。

    平台把论文按学科归档（filterField=subjects），直接按学科 ID 抓能精确
    锁定 工商管理 / 管理科学与工程 / 应用经济学，避免「最新列表」被一天几十篇
    图情档论文淹没、导致目标学科论文排不进前几页的问题。

    每个学科用独立 session 抓第 1 页（20 篇，时间倒序）。平台对高频请求
    会随机限流（连续抓后半段学科容易返回空），故学科间 sleep、单学科失败后
    重建 session 重试一次，尽量保证每个学科都能抓到。
    """
    all_papers, seen = [], set()
    for sid in subject_ids:
        html1 = None
        for attempt in range(2):
            sess = build_session()
            html1 = _get(sess, f"{SEARCH_URL}?type=filter&filterField=subjects&value={sid}")
            if html1:
                break
            print(f"[warn] 学科 {sid} 第 {attempt + 1} 次抓取失败，稍后重试", file=sys.stderr)
            time.sleep(3)
        if html1:
            for p in parse_list(html1):
                if p["id"] and p["id"] not in seen:
                    seen.add(p["id"])
                    all_papers.append(p)
        print(f"[info] 学科 {sid} 抓完，累计 {len(all_papers)} 篇", file=sys.stderr)
        time.sleep(3)
    return all_papers


# ---------------------------------------------------------------------------
def score_paper(p, keywords):
    title = p["title"]
    abstract = p["abstract"]
    hits, matched = 0, []
    exclude = keywords.get("exclude", [])

    # 排除词：命中标题或分类 → 直接淘汰（旅游/物流等；图情档已由学科白名单拦截）
    for ex in exclude:
        if ex in title or any(ex in c for c in p["category"]):
            return -1000, [f"排除:{ex}"]
    # 摘要命中 → 重罚（留极少高度相关论文一线机会）
    for ex in exclude:
        if ex in abstract:
            hits -= 15
            matched.append(f"排除:{ex}")

    def hit(k):
        nonlocal hits
        if k in title:
            return 3
        if k in abstract:
            return 1
        return 0

    for k in keywords["strong"]:
        s = hit(k)
        if s:
            hits += 2 * s
            matched.append(k)
    for k in keywords["weak"]:
        s = hit(k)
        if s:
            hits += s
            matched.append(k)
    for k in keywords["en"]:
        kl = k.lower()
        if kl in title.lower():
            hits += 6
            matched.append(k)
        elif kl in abstract.lower():
            hits += 2
            matched.append(k)

    return hits, matched


# ---------------------------------------------------------------------------
def build_html(recommended, all_papers, today):
    # ---- 推荐卡片 ----
    cards = []
    for i, p in enumerate(recommended, 1):
        authors = "、".join(p["authors"][:6])
        if len(p["authors"]) > 6:
            authors += " 等"
        cat = " / ".join(p["category"][:3]) or "—"
        abstract = p["abstract"] or "（摘要缺失）"
        if len(abstract) > 500:
            abstract = abstract[:500] + "…"
        why = "、".join(p["matched"][:8]) if p.get("matched") else "—"
        doi = p["doi"]
        doi_href = f"https://doi.org/{doi}"
        pdf = p["pdf_url"]

        if pdf:
            pdf_btn = (f'<a href="{html.escape(pdf)}" style="display:inline-block;background:#2e9e5b;'
                       f'color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;'
                       f'font-size:13px;font-weight:600;">📄 下载 PDF</a>')
        else:
            pdf_btn = (f'<a href="{html.escape(p["url"])}" style="display:inline-block;background:#b0b4ba;'
                       f'color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;'
                       f'font-size:13px;">PDF 待生成</a>')

        cards.append(f"""
        <div style="margin:20px 0;padding:16px;border:1px solid #e3e6ea;border-radius:8px;">
          <div style="font-size:16px;font-weight:700;color:#1a3c5e;line-height:1.5;">
            {i}. <a href="{html.escape(p['url'])}" style="color:#1a3c5e;text-decoration:none;">{html.escape(p['title'])}</a>
          </div>
          <div style="font-size:13px;color:#555;margin:8px 0;">
            作者：{html.escape(authors)}<br>
            分类：{html.escape(cat)} &nbsp;·&nbsp; 提交时间：{html.escape(p['date'] or '—')}
          </div>
          <div style="margin:10px 0;">{pdf_btn} &nbsp;
            <a href="{html.escape(doi_href)}" style="color:#2e6da4;font-size:13px;text-decoration:none;">DOI: {html.escape(doi)}</a>
          </div>
          <div style="font-size:13px;color:#2e6da4;margin:6px 0;">为什么推给你：命中 → {html.escape(why)}</div>
          <div style="font-size:13.5px;line-height:1.7;color:#333;margin-top:8px;">{html.escape(abstract)}</div>
        </div>""")

    # ---- 清单条目 ----
    list_items = []
    for p in all_papers:
        abstract = p["abstract"] or ""
        if len(abstract) > 150:
            abstract = abstract[:150] + "…"
        doi = p["doi"]
        list_items.append(f"""
        <li style="margin:10px 0;line-height:1.6;">
          <a href="{html.escape(p['url'])}" style="color:#1a3c5e;font-weight:600;text-decoration:none;">{html.escape(p['title'])}</a>
          <span style="color:#888;font-size:12px;">（{html.escape(p['date'] or '')}）</span>
          <a href="https://doi.org/{html.escape(doi)}" style="color:#2e6da4;font-size:12px;text-decoration:none;">{html.escape(doi)}</a>
          <br><span style="color:#555;font-size:13px;">{html.escape(abstract)}</span>
        </li>""")

    rec_html = "".join(cards) if cards else (
        '<p style="color:#888;">今日暂无与你研究兴趣高度相关的新论文。</p>')
    list_html = "".join(list_items) if list_items else (
        '<p style="color:#888;">今日暂无新论文。</p>')

    return f"""
    <div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;max-width:760px;margin:0 auto;">
      <div style="background:#a80602;color:#fff;padding:20px;border-radius:8px 8px 0 0;">
        <h2 style="margin:0;">哲社预印本日报</h2>
        <div style="font-size:13px;opacity:.9;margin-top:6px;">{FOCUS} · {today} · 推荐 {len(recommended)} 篇 / 清单 {len(all_papers)} 篇</div>
      </div>

      <div style="padding:4px 20px;">
        <h3 style="color:#a80602;border-bottom:2px solid #a80602;padding-bottom:6px;">🔍 为你推荐（{len(recommended)} 篇）</h3>
        {rec_html}
      </div>

      <div style="padding:4px 20px;">
        <h3 style="color:#a80602;border-bottom:2px solid #a80602;padding-bottom:6px;">📋 今日新论文清单（{len(all_papers)} 篇）</h3>
        <ul style="list-style:none;padding-left:0;">{list_html}</ul>
      </div>

      <div style="font-size:12px;color:#999;text-align:center;padding:16px;">
        数据来源：哲社预印本（zsyyb.cn，中国人民大学）· 仅推送论文清单，不做解读 · DOI 以 doi.org 为准
      </div>
    </div>
    """


def send_email(html_body, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER
    msg["To"] = RECEIVER
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    last_err = None
    try:
        s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=40)
        s.starttls(context=ssl.create_default_context())
        s.login(SENDER, SENDER_PASSWORD)
        s.sendmail(SENDER, [RECEIVER], msg.as_string())
        s.quit()
        print(f"[ok] 邮件已发送：{subject}")
        return
    except Exception as e:
        last_err = e
    try:
        s = smtplib.SMTP_SSL(SMTP_SERVER, 465, timeout=40, context=ssl.create_default_context())
        s.login(SENDER, SENDER_PASSWORD)
        s.sendmail(SENDER, [RECEIVER], msg.as_string())
        s.quit()
        print(f"[ok] 邮件已发送（SSL）：{subject}")
        return
    except Exception as e:
        last_err = e
    raise RuntimeError(f"邮件发送失败：{last_err}")


def main():
    keywords = load_json(KEYWORDS_FILE)
    subjects = load_json(SUBJECTS_FILE)
    subject_ids = list(subjects.get("subject_ids", {}).keys())
    allow = subjects.get("allow", [])
    exclude_subjects = subjects.get("exclude_subjects", [])
    state = load_state()
    sent = set(state.get("sent_ids", []))

    papers = fetch_papers(subject_ids)
    fresh = [p for p in papers if p["id"] not in sent]
    print(f"[info] 抓取 {len(papers)} 篇，去重后新论文 {len(fresh)} 篇", file=sys.stderr)

    if not fresh:
        subject = f"哲社预印本日报 · {cn_today()} · 今日暂无新论文"
        send_email("<p>今日暂无新论文发布。</p>", subject)
        return

    # 二次过滤：学科 ID 抓取后，再按 category 标签剔除混入的其它学科论文。
    # 论文常同时标多个学科，若命中目标学科(allow)但同时也命中非目标学科(exclude_subjects)，则剔除。
    eligible = [p for p in fresh if any(c in allow for c in p["category"])
                and not any(any(es in c for c in p["category"]) for es in exclude_subjects)]
    print(f"[info] 学科二次过滤后 {len(eligible)} 篇（仅限工商管理/管科/应用经济学）", file=sys.stderr)

    if not eligible:
        subject = f"哲社预印本日报 · {cn_today()} · 今日暂无相关学科论文"
        send_email("<p>今日暂无 工商管理 / 管理科学与工程 / 应用经济学 学科的新论文。</p>", subject)
        return

    # 清单：学科白名单内、最近两天、并剔除旅游/物流等排除词
    exclude = keywords.get("exclude", [])
    cutoff = cn_today() - dt.timedelta(days=1)
    checklist = []
    for p in eligible:
        if any(ex in p["title"] or any(ex in c for c in p["category"]) for ex in exclude):
            continue
        try:
            d = dt.date.fromisoformat(p["date"])
        except Exception:
            d = None
        if d is None or d >= cutoff:
            checklist.append(p)
    checklist = checklist[:LIST_MAX]

    if not checklist:
        subject = f"哲社预印本日报 · {cn_today()} · 今日暂无新论文"
        send_email("<p>今日暂无 工商管理 / 管理科学与工程 / 应用经济学 学科的新论文。</p>", subject)
        return

    # 推荐：在清单（当天/昨天新论文）里按关键词打分
    scored = []
    for p in checklist:
        hits, matched = score_paper(p, keywords)
        p["hits"], p["matched"] = hits, matched
        if hits > 0:
            scored.append(p)
    scored.sort(key=lambda p: p["hits"], reverse=True)
    recommended = scored[:TOP_N]

    print(f"[info] 推荐 {len(recommended)} 篇，清单 {len(checklist)} 篇", file=sys.stderr)

    today = cn_today().isoformat()
    subject = f"哲社预印本日报 · {today} · 推荐 {len(recommended)} 篇 / 新论文 {len(checklist)} 篇"
    send_email(build_html(recommended, checklist, today), subject)

    # 去重：把本次推送过的论文记入 sent（推荐 + 清单）
    pushed = {p["id"] for p in recommended} | {p["id"] for p in checklist}
    merged = list(dict.fromkeys(list(sent) + list(pushed)))[-5000:]
    save_state({"sent_ids": merged})
    print(f"[ok] 已更新去重状态，累计 {len(merged)} 条")


if __name__ == "__main__":
    main()
