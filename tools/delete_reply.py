"""Kanalın kendi yazdığı, verilen metni içeren yorum cevaplarını siler (son 50 yorum dizisi).
    python tools/delete_reply.py "metin parçası"
"""
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

needle = sys.argv[1].lower()
c = Credentials(None, refresh_token=os.environ['YT_REFRESH_TOKEN'], client_id=os.environ['YT_CLIENT_ID'],
                client_secret=os.environ['YT_CLIENT_SECRET'], token_uri='https://oauth2.googleapis.com/token')
c.refresh(Request())
yt = build('youtube', 'v3', credentials=c, cache_discovery=False)
cid = yt.channels().list(part='id', mine=True).execute()['items'][0]['id']
threads = yt.commentThreads().list(part='snippet,replies', allThreadsRelatedToChannelId=cid, maxResults=50,
                                   order='time', textFormat='plainText').execute().get('items', [])
n = 0
for t in threads:
    for r in t.get('replies', {}).get('comments', []):
        sn = r['snippet']
        if sn.get('authorChannelId', {}).get('value') == cid and needle in (sn.get('textOriginal') or sn.get('textDisplay', '')).lower():
            yt.comments().delete(id=r['id']).execute()
            n += 1
            print(f"silindi: {sn.get('textOriginal') or sn.get('textDisplay')}")
print(f'{n} cevap silindi')
