"""YouTube yükleme yardımcıları. Secrets: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (+ YT_CHANNEL_ID)."""
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# tam 'youtube' izni: yükleme + oynatma listesi + kanal ayarları (ileride kanal ayarları)
SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.force-ssl']  # force-ssl: yorum cevapları


class QuotaError(Exception):
    pass


def configured():
    return all(os.environ.get(k) for k in ('YT_CLIENT_ID', 'YT_CLIENT_SECRET', 'YT_REFRESH_TOKEN'))


def client():
    creds = Credentials(None, refresh_token=os.environ['YT_REFRESH_TOKEN'], client_id=os.environ['YT_CLIENT_ID'],
                        client_secret=os.environ['YT_CLIENT_SECRET'], token_uri='https://oauth2.googleapis.com/token')
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)


def add_to_playlist(playlist_id, video_id):
    client().playlistItems().insert(part='snippet', body={'snippet': {
        'playlistId': playlist_id, 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()


def check_channel():
    items = client().channels().list(part='id,snippet', mine=True).execute().get('items', [])
    if not items:
        raise RuntimeError('this token has no YouTube channel')
    cid, title = items[0]['id'], items[0]['snippet']['title']
    want = os.environ.get('YT_CHANNEL_ID')
    if want and cid != want:
        raise RuntimeError(f'token belongs to {title} ({cid}), expected {want}')
    return f'{title} ({cid})'


def clean(s):
    """YouTube başlık/açıklamada < ve > kabul etmez."""
    return s.replace('->', '→').replace('<', '').replace('>', '')


def upload(mp4, title, description, tags, privacy, category='23'):
    title, description = clean(title)[:100], clean(description)[:4900]
    tags = [clean(t) for t in tags]
    body = {
        'snippet': {'title': title, 'description': description, 'tags': tags, 'categoryId': category},
        'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': False},
    }
    req = client().videos().insert(part='snippet,status', body=body,
                                   media_body=MediaFileUpload(str(mp4), mimetype='video/mp4', resumable=True, chunksize=-1))
    try:
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
    except HttpError as e:
        if e.resp.status == 403 and ('quotaExceeded' in str(e) or 'uploadLimitExceeded' in str(e)):
            raise QuotaError(str(e))
        raise
    return resp['id']


def set_thumbnail(video_id, png):
    client().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(png), mimetype='image/png')).execute()


def locked_videos(ids):
    """Public yüklenip YouTube tarafından private'a çekilenler (API denetimi gerekiyorsa olur)."""
    if not ids:
        return []
    items = client().videos().list(part='status', id=','.join(ids)).execute().get('items', [])
    return [(v['id'], v['status'].get('privacyStatus')) for v in items if v['status'].get('privacyStatus') != 'public']
