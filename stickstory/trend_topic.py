"""Trend tabanlı konu (2026-10-12, kullanıcı: "trendleri her zaman çek, videolar ona göre oluşsun, alakalı olsun").

Kaynaklar (ikisi de herkese açık): Google Trends günlük aramaları (ülke RSS) + YouTube arama kutusu önerileri.
Gemini, kanalın konusuna gerçekten uyan ve güvenli TEK bir trendi seçip onu genel bir günlük-hayat konusuna çevirir;
uyan yoksa NONE der ve normal konu kullanılır. Gerçek kişi, siyaset, suç, felaket, sağlık korkusu, marka, spor sonucu
gibi trendler asla seçilmez (YouTube kuralları + kanal tonu).
"""
import json
import xml.etree.ElementTree as ET

import requests

RULES = ("Never pick anything about: real people or celebrities, politics, elections, wars, crime, accidents, disasters, "
         "deaths, illness or health scares, religion, brands or trademarks, sports matches or results, children, tragedies "
         "or any real news event. Do not mock real events. Only everyday-life themes that are harmless and fun "
         "(seasons, holidays, food crazes, school/work life, home life, weather, hobbies, internet habits).")


def fetch(geo='US', seeds=(), hl='en', limit=35):
    out = []   # önce kanalın arama önerileri (alakalı), sonra günün genel trendleri
    for q in seeds:
        try:
            r = requests.get('https://suggestqueries.google.com/complete/search',
                             params={'client': 'firefox', 'ds': 'yt', 'hl': hl, 'q': q}, timeout=10)
            out += r.json()[1][1:8]
        except Exception:
            pass
    try:
        r = requests.get('https://trends.google.com/trending/rss', params={'geo': geo}, timeout=10)
        out += [i.findtext('title') for i in ET.fromstring(r.content).iter('item')][:15]
    except Exception:
        pass
    seen, uniq = set(), []
    for t in out:
        k = (t or '').strip().lower()
        if k and k not in seen:
            seen.add(k); uniq.append(t.strip())
    return uniq[:limit]


def pick(gemini, parse_json, channel, trends, used, lang='English'):
    """-> (konu, trend) ya da (None, None). gemini/parse_json: kanalın kendi yardımcıları."""
    if not trends:
        return None, None
    prompt = (f"You pick video topics for this YouTube Shorts channel: {channel}\n"
              f"Today's trending searches: {json.dumps(trends, ensure_ascii=False)}\n"
              f"Recent topics to avoid: {json.dumps(sorted(t for t in used if t)[:30], ensure_ascii=False)}\n\n"
              "Choose at most ONE trend that genuinely fits this channel's comedy and turn it into a short, generic "
              f"everyday topic phrase in {lang} (3-7 words) that the video can be about. {RULES}\n"
              "If nothing fits naturally, answer NONE - forcing an unrelated trend is worse than skipping.\n"
              'JSON only: {"trend": "<the trend you used>", "topic": "<topic phrase>"} or {"topic": "NONE"}')
    try:
        j = parse_json(gemini(prompt, temperature=0.4))
    except Exception as e:
        print(f'[trend] atlandı: {e}', flush=True)
        return None, None
    topic = (j.get('topic') or '').strip() if isinstance(j, dict) else ''
    if not topic or topic.upper() == 'NONE' or len(topic) > 80:
        return None, None
    return topic, (j.get('trend') or '').strip()
