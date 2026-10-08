"""Bir kez, kendi bilgisayarında: çöp adam kanalının YouTube yetkisini al.

1. Google Cloud Console -> Credentials -> OAuth client ID (Desktop app) JSON'u bu klasöre client_secret.json olarak koy
   (git'e girmez). Önceki kanallarının projesindeki aynı dosya kullanılabilir.
2. python auth_setup.py --repo <kullanıcı>/<repo>
3. Tarayıcıda Gmail'i, sonra çöp adam kanalını seç.
4. --repo verilirse değerler doğrudan GitHub Secrets'a yazılır (ekrana basılmaz); verilmezse ekrana yazılır.
"""
import argparse, subprocess, sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload import SCOPES  # noqa: E402

SECRET = Path(__file__).resolve().parent / 'client_secret.json'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='YT_', help='secret adı ön eki (ör. YT_EN_)')
    ap.add_argument('--repo', help='GitHub repo (owner/name): secrets gh CLI ile yazılır')
    ap.add_argument('--expect', default='', help='beklenen kanal adı (parçası), boşsa kontrol yok')
    a = ap.parse_args()
    if not SECRET.exists():
        sys.exit(f'missing {SECRET} (Google Cloud Console’dan indir)')
    flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt='consent select_account', access_type='offline')
    yt = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    import time
    for attempt in range(8):  # yeni verilen izin YouTube'a birkaç saniye geç ulaşabiliyor (401)
        try:
            items = yt.channels().list(part='id,snippet', mine=True).execute().get('items', [])
            break
        except Exception as e:  # noqa: BLE001
            if '401' not in str(e) or attempt == 7:
                raise
            print('YouTube izni henüz yansımadı, bekleniyor...', flush=True)
            time.sleep(15)
    if not items:
        sys.exit('bu hesapta YouTube kanalı yok')
    cid, title = items[0]['id'], items[0]['snippet']['title']
    print(f'\nKanal: {title} ({cid})')
    if a.expect and a.expect.lower() not in title.lower():
        sys.exit(f"Bu '{a.expect}' değil. Tekrar çalıştır ve doğru kanalı seç. Hiçbir şey kaydedilmedi.")
    values = {f'{a.prefix}CLIENT_ID': creds.client_id, f'{a.prefix}CLIENT_SECRET': creds.client_secret,
              f'{a.prefix}REFRESH_TOKEN': creds.refresh_token, f'{a.prefix}CHANNEL_ID': cid}
    if a.repo:
        for k, v in values.items():
            subprocess.run(['gh', 'secret', 'set', k, '--repo', a.repo], input=v, text=True, check=True)
        print(f'GitHub secrets yazıldı: {", ".join(values)} -> {a.repo}')
    else:
        for k, v in values.items():
            print(f'{k:17}= {v}')


if __name__ == '__main__':
    main()
