"""Aynı başlıkla iki kez yüklenmiş herkese açık videoları bulur; en çok izleneni kalır, diğerleri GİZLİ yapılır
(silinmez, Studio'dan geri açılabilir). DRY=1 sadece listeler."""
import os, re, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_reply import creds  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

yt = build('youtube', 'v3', credentials=creds(), cache_discovery=False)
up = yt.channels().list(part='contentDetails', mine=True).execute()['items'][0]['contentDetails']['relatedPlaylists']['uploads']
ids, req = [], yt.playlistItems().list(part='contentDetails', playlistId=up, maxResults=50)
while req:
    r = req.execute(); ids += [i['contentDetails']['videoId'] for i in r['items']]
    req = yt.playlistItems().list_next(req, r)
groups = defaultdict(list)
for k in range(0, len(ids), 50):
    for v in yt.videos().list(part='snippet,statistics,status', id=','.join(ids[k:k + 50])).execute()['items']:
        if v['status']['privacyStatus'] == 'public':
            groups[re.sub(r'\W+', ' ', v['snippet']['title']).strip().lower()].append(v)
for title, vs in groups.items():
    if len(vs) < 2:
        continue
    vs.sort(key=lambda v: int(v['statistics'].get('viewCount', 0)), reverse=True)
    print(f'KOPYA: "{vs[0]["snippet"]["title"]}" → kalan {vs[0]["id"]} ({vs[0]["statistics"].get("viewCount")} izl)')
    for v in vs[1:]:
        print(f'   gizle {v["id"]} ({v["statistics"].get("viewCount")} izl, {v["snippet"]["publishedAt"][:16]})')
        if os.environ.get('DRY') != '1':
            st = v['status']; st['privacyStatus'] = 'private'
            yt.videos().update(part='status', body={'id': v['id'], 'status': {k: st[k] for k in (
                'privacyStatus', 'selfDeclaredMadeForKids', 'embeddable', 'license', 'publicStatsViewable') if k in st}}).execute()
            print('   ✓ gizlendi')
