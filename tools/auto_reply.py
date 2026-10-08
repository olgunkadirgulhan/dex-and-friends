"""Yorumlara otomatik, kişisel cevap + yeni videolara ilk yorum (GitHub Actions 'comments' iş akışı, saatte bir).

Ayrıca: son 48 saatte yayınlanan ve kanalın henüz yorum yazmadığı her videoya, o videoya özel bir soru ile
"ilk yorum" (FIRST_COMMENT=1, CHANNEL_LANG=en|tr|de). "Like/subscribe" ve link yok.

Kurallar (YouTube spam politikasına takılmamak için):
  - Her cevap yoruma özel ve yorumun dilinde (Gemini yazar; yoksa çeşitli hazır kalıplar), kısa, 1-2 emoji
  - Link / reklam / hakaret / çok uzun yorumlara cevap yok; kanalın kendi yorumlarına ve zaten cevaplananlara yok
  - Son 7 günün yorumları, çalışma başına en fazla MAX_PER_RUN cevap; her yoruma bir kez (comment_replies.json)
  - CHANNEL_KIND=finance: soru gelse de tavsiye yok, sadece teşekkür
Env: YT_CLIENT_ID/SECRET/REFRESH_TOKEN (ya da YOUTUBE_*), GEMINI_API_KEY (opsiyonel), CHANNEL_NAME, CHANNEL_ABOUT,
     CHANNEL_KIND (general|finance), STATE_FILE (varsayılan comment_replies.json), MAX_PER_RUN (varsayılan 10)
Gerekli OAuth izni: youtube.force-ssl (yoksa uyarı verip çıkar → auth_setup.py ile yeniden bağlan).
"""
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

STATE = Path(os.environ.get('STATE_FILE', 'comment_replies.json'))
MAX = int(os.environ.get('MAX_PER_RUN', '10'))
KIND = os.environ.get('CHANNEL_KIND', 'general')
NAME = os.environ.get('CHANNEL_NAME', 'our channel')
ABOUT = os.environ.get('CHANNEL_ABOUT', '')
SKIP = re.compile(r'(https?://|www\.|\.com\b|t\.me|telegram|whatsapp|wa\.me|check my|my channel|sub4sub|subscribe to me|'
                  r'kanalıma|abone ol(ur)?(san|musun)|promo|giveaway|airdrop|dm me|contact me|investment manager)', re.I)
RUDE = re.compile(r'\b(fuck|shit|bitch|idiot|stupid|scam|amk|aq|siktir|orospu|salak|aptal|gerizekalı)\b', re.I)
TR = re.compile(r'[çğıöşüÇĞİÖŞÜ]|\b(çok|güzel|harika|teşekkür|sağol|bir|ve|bu|ne|mi)\b', re.I)
TEMPLATES = {
    'tr': ['Çok teşekkürler! 🙌', 'Beğenmene çok sevindik 😊', 'Yorumun için teşekkürler! 💛', 'Harika, iyi ki varsın! 🎉',
           'Desteğin için teşekkürler 🙏😊', 'Sevindik! Yeni videolar yolda 🚀'],
    'en': ['Thanks so much! 🙌', 'So glad you enjoyed it 😊', 'Thank you for watching! 💛', 'Love this, thanks! 🎉',
           'Appreciate you! 🙏😊', 'Thanks! More videos coming soon 🚀'],
    'de': ['Vielen Dank! 🙌', 'Freut uns sehr 😊', 'Danke fürs Zuschauen! 💛', 'Super, danke dir! 🎉'],
}


def env2(a, b):
    return os.environ.get(a) or os.environ.get(b, '')


def creds():
    c = Credentials(None, refresh_token=env2('YT_REFRESH_TOKEN', 'YOUTUBE_REFRESH_TOKEN'),
                    client_id=env2('YT_CLIENT_ID', 'YOUTUBE_CLIENT_ID'),
                    client_secret=env2('YT_CLIENT_SECRET', 'YOUTUBE_CLIENT_SECRET'),
                    token_uri='https://oauth2.googleapis.com/token')
    c.refresh(Request())
    return c


def has_force_ssl(c) -> bool:
    r = requests.get('https://oauth2.googleapis.com/tokeninfo', params={'access_token': c.token}, timeout=20)
    return 'youtube.force-ssl' in r.json().get('scope', '')


def lang(text):
    if TR.search(text):
        return 'tr'
    if re.search(r'[äöüß]|\b(danke|sehr|gut|ich|und|das)\b', text, re.I):
        return 'de'
    return 'en'


def gemini(comment: str) -> str | None:
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        return None
    rules = ('Never give financial advice, price predictions or opinions on buying/selling; if asked, just thank '
             'them and say it is not financial advice. ' if KIND == 'finance' else '')
    prompt = (f'You are the friendly creator of the YouTube channel "{NAME}". {ABOUT}\n'
              f'Write a reply to this viewer comment in the SAME language as the comment. Max 15 words, warm and '
              f'specific to what they said, 1-2 emojis. No links, no hashtags, no asking for subscriptions, no promises. '
              f'{rules}Be honest: never claim how the videos are made, never pretend to be a person doing human things '
              f'(homework, drawing by hand, brainstorming), never deny that AI is used. If asked how it is made, say '
              f'it is made with the help of AI tools. If the comment is negative, reply politely and briefly. '
              f'Output only the reply text.\n\n'
              f'Comment: """{comment[:500]}"""')
    for model in ('gemini-flash-latest', 'gemini-flash-lite-latest', 'gemini-3.5-flash'):
        try:
            r = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                              json={'contents': [{'parts': [{'text': prompt}]}],
                                    'generationConfig': {'temperature': 0.9}},
                              headers={'x-goog-api-key': key}, timeout=40)
            if r.status_code != 200:
                continue
            parts = r.json()['candidates'][0]['content']['parts']
            text = ''.join(p.get('text', '') for p in parts if not p.get('thought')).strip().strip('"')
            if text and not SKIP.search(text) and len(text) <= 220:
                return text
        except Exception:  # noqa: BLE001
            continue
    return None


LANG = os.environ.get('CHANNEL_LANG', 'en')
FIRST = {
    'en': ['Which part made you laugh the most? 😄', 'Did you see that coming? 👀', 'What should we make next? 💡',
           'Be honest, how many did you get right? 🤔', 'Rate this one from 1 to 10 👇'],
    'tr': ['En çok hangi kısım güldürdü? 😄', 'Bunu bekliyor muydun? 👀', 'Sıradaki video ne olsun? 💡',
           'Dürüst ol, kaçını bildin? 🤔', 'Bu videoya 1-10 arası puan ver 👇'],
}


def topic_of(title: str) -> str:
    """Başlıktan konu: '5 German words: Family 👨‍👩‍👧 | A1' → 'Family', '... dialogue? At the café ☕' → 'At the café'."""
    t = re.sub(r'#\S+', '', title)
    parts = [p.strip() for p in re.split(r'\s[|·]\s', t)]
    for p in parts[1:]:  # '| Konut ve kira' gibi konu etiketi (seviye etiketi değilse)
        if not re.fullmatch(r'(level\s)?[ABC][12]', p, re.I) and 2 < len(p) < 30:
            return _clean(p)
    main = parts[0]
    if '?' in main and len(main.split('?', 1)[1].strip()) > 2:
        main = main.split('?', 1)[1]
    elif ':' in main:
        left, right = main.split(':', 1)
        generic = re.compile(r'diyalog|dialogue|conversation|quiz|words|kelime|vs\b|level', re.I)
        main = left if (generic.search(right) and not generic.search(left)) or len(right.strip()) <= 2 else right
    main = re.split('[!?\U0001F000-\U0001FFFF\u2600-\u27BF]', main)[0]  # emoji/!/? öncesi
    return _clean(main)


def _clean(t: str) -> str:
    t = re.sub('[\U0001F000-\U0001FFFF\u2600-\u27BF\u200d\ufe0f]', '', t)
    t = re.sub(r'\b(Edition|Glow-Up)\b', '', t)
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s&\'-]', ' ', t)).strip()[:40]


def pick_question(pool: list, title: str, state: dict) -> str:
    """Havuzdan, son 6 kullanılanı tekrar etmeden; {topic} başlıktan doldurulur."""
    recent = state.setdefault('recent_q', [])
    topic = topic_of(title)
    cands = [q for q in pool if q not in recent and ('{topic}' not in q or topic)] or pool
    q = random.choice(cands)
    state['recent_q'] = (recent + [q])[-6:]
    return q.replace('{topic}', topic)


def first_question(title: str, desc: str, state: dict | None = None) -> str:
    key = os.environ.get('GEMINI_API_KEY')
    if key:
        rules = ('It is a finance channel: ask for an opinion (e.g. will the move continue?), never ask what to buy '
                 'and never give advice. ' if KIND == 'finance' else '')
        prompt = (f'You run the YouTube channel "{NAME}". {ABOUT}\nWrite the first comment under your new Short, in '
                  f'{"Turkish" if LANG == "tr" else "German" if LANG == "de" else "English"}: ONE short question (max 14 '
                  f'words) about THIS video that viewers can answer in a few words, plus 1 emoji. No "like", '
                  f'"subscribe", links or hashtags. {rules}Output only the comment.\n\nVideo title: {title}\n'
                  f'Description: {desc[:400]}')
        for model in ('gemini-flash-latest', 'gemini-flash-lite-latest', 'gemini-3.5-flash'):
            try:
                r = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                                  json={'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 0.9}},
                                  headers={'x-goog-api-key': key}, timeout=40)
                if r.status_code == 200:
                    parts = r.json()['candidates'][0]['content']['parts']
                    t = ''.join(p.get('text', '') for p in parts if not p.get('thought')).strip().strip('"')
                    if t and not SKIP.search(t) and len(t) <= 160 and not re.search(r'subscri|abone|like', t, re.I):
                        return t
            except Exception:  # noqa: BLE001
                continue
    pool = FIRST.get(LANG, FIRST['en'])
    custom = os.environ.get('FIRST_QUESTIONS')  # kanala özel soru havuzu (JSON liste), Gemini anahtarı yoksa
    if custom:
        try:
            pool = json.loads(custom)
        except Exception:  # noqa: BLE001
            pass
    return pick_question(pool, title, state if state is not None else {})


def first_comments(yt, cid, state):
    """Son 48 saatte yüklenen ve kanalın henüz yorum yazmadığı her videoya, videoya özel bir soru."""
    uploads = yt.channels().list(part='contentDetails', id=cid).execute()['items'][0]['contentDetails'][
        'relatedPlaylists']['uploads']
    items = yt.playlistItems().list(part='contentDetails', playlistId=uploads, maxResults=10).execute().get('items', [])
    ids = [i['contentDetails']['videoId'] for i in items]
    if not ids:
        return
    vids = yt.videos().list(part='snippet,status', id=','.join(ids)).execute().get('items', [])
    since = datetime.now(timezone.utc) - timedelta(hours=48)
    done = set(state.setdefault('first', []))
    for v in vids:
        if v['id'] in done or v['status'].get('privacyStatus') != 'public':
            continue
        if datetime.fromisoformat(v['snippet']['publishedAt'].replace('Z', '+00:00')) < since:
            done.add(v['id']); continue
        try:
            th = yt.commentThreads().list(part='snippet', videoId=v['id'], maxResults=50).execute().get('items', [])
        except Exception as e:  # noqa: BLE001 — yorumları kapalı video
            print(f"{v['id']}: yorumlar kapalı ({str(e)[:80]})"); done.add(v['id']); continue
        if any(t['snippet']['topLevelComment']['snippet'].get('authorChannelId', {}).get('value') == cid for t in th):
            done.add(v['id']); continue
        q = first_question(v['snippet']['title'], v['snippet'].get('description', ''), state)
        yt.commentThreads().insert(part='snippet', body={'snippet': {'videoId': v['id'], 'topLevelComment': {
            'snippet': {'textOriginal': q}}}}).execute()
        done.add(v['id'])
        print(f"💬 ilk yorum {v['id']}: {q}")
    state['first'] = list(done)[-500:]


def main():
    c = creds()
    if not has_force_ssl(c):  # tokeninfo bazen eksik kapsam döndürüyor → sadece uyarı, işlem denenir
        print('ℹ️ tokeninfo force-ssl göstermedi; deneniyor (403 gelirse auth_setup.py ile yeniden bağlan)')
    yt = build('youtube', 'v3', credentials=c, cache_discovery=False)
    cid = yt.channels().list(part='id', mine=True).execute()['items'][0]['id']
    state = json.loads(STATE.read_text()) if STATE.exists() else {'replied': []}
    if os.environ.get('FIRST_COMMENT', '1') == '1':
        try:
            first_comments(yt, cid, state)
        except Exception as e:  # noqa: BLE001 — ilk yorum hatası cevapları durdurmasın
            print(f'ilk yorum hatası: {str(e)[:200]}')
    done = set(state['replied'])
    since = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        threads = yt.commentThreads().list(part='snippet,replies', allThreadsRelatedToChannelId=cid, maxResults=50,
                                           order='time', textFormat='plainText').execute().get('items', [])
    except Exception as e:  # noqa: BLE001 — yorumlar kapalı kanal vb.
        print(f'yorumlar alınamadı: {e}'); return
    sent = 0
    for t in threads:
        if sent >= MAX:
            break
        top = t['snippet']['topLevelComment']
        sn = top['snippet']
        if top['id'] in done or sn.get('authorChannelId', {}).get('value') == cid:
            continue
        if datetime.fromisoformat(sn['publishedAt'].replace('Z', '+00:00')) < since:
            continue
        replies = t.get('replies', {}).get('comments', [])
        if any(r['snippet'].get('authorChannelId', {}).get('value') == cid for r in replies):
            done.add(top['id']); continue
        text = sn.get('textDisplay') or sn.get('textOriginal') or ''
        if not text.strip() or len(text) > 600 or SKIP.search(text) or RUDE.search(text):
            done.add(top['id']); continue
        reply = gemini(text) or random.choice(TEMPLATES[lang(text)])
        yt.comments().insert(part='snippet', body={'snippet': {'parentId': top['id'], 'textOriginal': reply}}).execute()
        done.add(top['id']); sent += 1
        print(f'↩︎ "{text[:60]}" → "{reply}"')
    state['replied'] = list(done)[-2000:]
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + '\n')
    print(f'{sent} cevap gönderildi')


if __name__ == '__main__':
    sys.exit(main())
