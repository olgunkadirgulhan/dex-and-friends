"""Kanal yorum teşhisi: son videoların istatistikleri, görünen/bekleyen/spam yorumlar, bizim yorumlarımızın durumu."""
import os, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_reply import creds  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

yt = build('youtube', 'v3', credentials=creds(), cache_discovery=False)
ch = yt.channels().list(part='id,statistics,contentDetails,status', mine=True).execute()['items'][0]
cid = ch['id']
print('KANAL', ch['statistics'], ch.get('status', {}).get('madeForKids'))
up = ch['contentDetails']['relatedPlaylists']['uploads']
ids = [i['contentDetails']['videoId'] for i in yt.playlistItems().list(part='contentDetails', playlistId=up, maxResults=30).execute()['items']]
vs = yt.videos().list(part='snippet,statistics,status', id=','.join(ids)).execute()['items']
tot = Counter()
for v in vs:
    s = v['statistics']; vid = v['id']
    seen = mine = 0
    try:
        req = yt.commentThreads().list(part='snippet,replies', videoId=vid, maxResults=100, textFormat='plainText')
        while req:
            r = req.execute()
            for t in r['items']:
                seen += 1 + t['snippet'].get('totalReplyCount', 0)
                for c in [t['snippet']['topLevelComment']] + t.get('replies', {}).get('comments', []):
                    mine += c['snippet'].get('authorChannelId', {}).get('value') == cid
            req = yt.commentThreads().list_next(req, r)
    except Exception as e:
        seen = f'HATA {str(e)[:80]}'
    held = {}
    for st in ('heldForReview', 'likelySpam'):
        try:
            held[st] = len(yt.commentThreads().list(part='id', videoId=vid, moderationStatus=st, maxResults=100).execute()['items'])
        except Exception as e:
            held[st] = 'x:' + str(e)[:60]
    tot['views'] += int(s.get('viewCount', 0)); tot['api'] += int(s.get('commentCount', 0))
    print(f"{v['snippet']['publishedAt'][:16]} {int(s.get('viewCount',0)):>8} izl {s.get('likeCount','?'):>5} beğ  "
          f"yorumSayısı={s.get('commentCount','?'):>4} görünen={seen} bizim={mine} bekleyen={held['heldForReview']} spam={held['likelySpam']} "
          f"kids={v['status'].get('madeForKids')} {v['status']['privacyStatus']} {v['snippet']['title'][:45]}")
print('TOPLAM', dict(tot))
