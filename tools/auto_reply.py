"""Yorumlara otomatik, kişisel cevap (GitHub Actions 'comments' iş akışı, 3 saatte bir).

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
              f'{rules}If the comment is negative, reply politely and briefly. Output only the reply text.\n\n'
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


def main():
    c = creds()
    if not has_force_ssl(c):
        print('⚠️ Bu bağlantıda yorum izni (youtube.force-ssl) yok: auth_setup.py ile yeniden bağlan. Atlandı.')
        return
    yt = build('youtube', 'v3', credentials=c, cache_discovery=False)
    cid = yt.channels().list(part='id', mine=True).execute()['items'][0]['id']
    state = json.loads(STATE.read_text()) if STATE.exists() else {'replied': []}
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
