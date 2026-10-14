"""StickStory pipeline. Bir çalıştırma = bir video.

    senaryo (Gemini yazar + hakem, yoksa bank/) -> ses + animasyon render -> YouTube'a yükle -> kaydet

Kullanım
  python run.py                    # 1 Shorts
  python run.py --format long      # 1 uzun video (~4 dk, Gemini gerekir)
  python run.py --no-upload        # sadece render (output/<id>/video.mp4)
  python run.py --script bank/001_birthday_cake.json --no-upload

Env
  GEMINI_API_KEY   senaryo üretimi (yoksa bank/ kullanılır)
  YT_PRIVACY       public | private | unlisted | off  (varsayılan: YT secrets varsa private, yoksa off)
  MAX_PER_DAY      Shorts günlük üst sınırı (varsayılan 3)
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN, YT_CHANNEL_ID
Yüklemesi başarısız olan video queue/<id>.json'a yazılır, sonraki çalıştırmada aynı senaryo yeniden render edilip yüklenir.
"""
import argparse
import csv
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from stickstory import render, writer  # noqa: E402
from stickstory.cast import CAST  # noqa: E402

HIST = HERE / 'history.json'
PUBLISHED = HERE / 'published.csv'
QUEUE = HERE / 'queue'
OUT = HERE / 'output'
FIELDS = ['id', 'date_utc', 'format', 'video_id', 'privacy', 'template', 'source', 'score', 'title']
BASE_TAGS = ['animation', 'cartoon', 'funny', 'comedy', 'stick figure', 'animated story', 'relatable']


def log(msg):
    print(f'[run] {msg}', flush=True)


def gh_annotation(level, msg):
    print(f'::{level}::{msg}' if os.environ.get('GITHUB_ACTIONS') else f'[run] {level.upper()}: {msg}', flush=True)


def published_rows():
    if not PUBLISHED.exists():
        return []
    with PUBLISHED.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def uploaded_today(fmt):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return sum(1 for r in published_rows() if r['date_utc'].startswith(today) and r.get('format', 'short') == fmt)


def record(sc, video_id, privacy):
    new = not PUBLISHED.exists()
    with PUBLISHED.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({'id': sc['id'], 'date_utc': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
                    'format': sc['format'], 'video_id': video_id, 'privacy': privacy,
                    'template': sc.get('template'), 'source': sc.get('source'), 'score': sc.get('judge_score', ''),
                    'title': sc['title']})


def remember(hist, sc):
    hist['recent'].append({'id': sc['id'], 'format': sc['format'], 'template': sc.get('template'),
                           'topic': sc.get('topic'), 'title': sc.get('title'),
                           'date': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})
    hist['recent'] = hist['recent'][-120:]
    if sc.get('bank_id'):
        hist.setdefault('bank_used', []).append(sc['bank_id'])
    HIST.write_text(json.dumps(hist, indent=2, ensure_ascii=False), encoding='utf-8')


def metadata(sc):
    names = sorted({CAST[c['id']]['name'] for S in sc['scenes'] for c in S['characters']})
    tags = BASE_TAGS + [t for t in sc.get('tags', []) if isinstance(t, str)]
    tags += [n.lower() for n in names]
    tag_by_template = {'caught': '#caughtlying', 'mom_logic': '#momlogic', 'expectation_reality': '#expectationvsreality',
                       'types_of_people': '#typesofpeople', 'plot_twist': '#plottwist'}
    extra = tag_by_template.get(sc.get('template'), '#cartoon')
    hashtags = (f'#shorts #DexAndFriends #animation #comedy #funny {extra}' if sc['format'] == 'short'
                else '#DexAndFriends #animation #comedy #cartoon #animatedseries')
    desc = (f"{sc.get('description', '')}\n\n"
            f"Starring: {', '.join(names)}\n"
            'New animated comedy every day. Subscribe so you never miss what Dex does next!\n\n'
            f'{hashtags}')
    title = sc['title']
    seen, uniq = set(), []
    for t in tags:
        if t.lower() not in seen and sum(len(x) for x in uniq) + len(t) < 450:
            seen.add(t.lower()); uniq.append(t)
    return title, desc, uniq


def social(mp4, title, url, sc):
    """Short → social/ (artifact 'social-<run>'): fenek-shorts sosyal yayıncısı Instagram/TikTok'a koyar."""
    import shutil
    soc = HERE / 'social'
    soc.mkdir(exist_ok=True)
    shutil.copy(mp4, soc / 'video.mp4')
    tiktok = f"{title}\n\nTag someone who does this 👇\n\n#dexandfriends #animation #comedy #relatable #funny #fyp"
    insta = (f"{title}\n\nTag someone who does this 👇\nFollow for a new Dex episode every day\n\n"
             "#animation #comedy #funny #relatable #cartoon")  # Instagram: en fazla 5 etiket
    (soc / 'post.json').write_text(json.dumps({'channel': 'Dex & Friends', 'title': title, 'url': url,
                                               'tiktok': tiktok, 'instagram': insta}, ensure_ascii=False, indent=1),
                                   encoding='utf-8')


def privacy_mode(no_upload):
    import upload
    if no_upload:
        return 'off'
    if not upload.configured():
        gh_annotation('warning', 'YouTube secrets missing: render only. Run auth_setup.py to connect the channel.')
        return 'off'
    mode = (os.environ.get('YT_PRIVACY') or '').strip().lower()
    if mode not in ('public', 'private', 'unlisted', 'off'):
        mode = 'private'
    return mode


def enqueue(sc, error, qpath=None):
    QUEUE.mkdir(exist_ok=True)
    p = qpath or QUEUE / f"{sc['id']}.json"
    prev = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    p.write_text(json.dumps({'scenario': sc, 'attempts': prev.get('attempts', 0) + 1, 'last_error': str(error)[:500]},
                            indent=2, ensure_ascii=False), encoding='utf-8')
    log(f"queued {sc['id']} for retry")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--format', choices=['short', 'long'], default='short')
    ap.add_argument('--no-upload', action='store_true')
    ap.add_argument('--script', help='belirli bir senaryo JSON dosyası')
    args = ap.parse_args()
    import upload

    fmt = args.format
    mode = privacy_mode(args.no_upload)
    log(f'format {fmt} | upload mode: {mode}')
    if mode != 'off':
        cap = int(os.environ.get('MAX_PER_DAY') or 3) if fmt == 'short' else 1
        if uploaded_today(fmt) >= cap:
            log(f'daily cap of {cap} {fmt} reached, nothing to do'); return
        try:
            log(f'channel check ok: {upload.check_channel()}')
        except Exception as e:
            gh_annotation('error', f'channel check failed, nothing uploaded: {e}'); raise SystemExit(1)

    hist = writer.load_hist(HIST)
    qpath = None
    queued = sorted(p for p in QUEUE.glob('*.json')) if QUEUE.exists() else []
    queued = [p for p in queued if json.loads(p.read_text(encoding='utf-8'))['scenario'].get('format') == fmt]
    if args.script:
        sc = json.loads(Path(args.script).read_text(encoding='utf-8'))
        writer.normalize(sc, sc.get('format', fmt))
        sc.setdefault('source', 'file')
    elif queued:
        qpath = queued[0]
        sc = json.loads(qpath.read_text(encoding='utf-8'))['scenario']
        log(f"retrying queued {sc['id']}")
    else:
        sc = writer.make_script(fmt, hist)
    log(f"script [{sc.get('source')}] {sc.get('template')} | {sc['title']}")

    out = OUT / sc['id']
    out.mkdir(parents=True, exist_ok=True)
    (out / 'script.json').write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding='utf-8')
    try:
        mp4, dur = render.render(sc, out, preview_png=out / 'thumb.png')
    except Exception as e:
        traceback.print_exc(); gh_annotation('error', f'render failed: {e}'); raise SystemExit(1)
    title, desc, tags = metadata(sc)
    (out / 'meta.json').write_text(json.dumps({'title': title, 'description': desc, 'tags': tags, 'duration': dur},
                                              indent=2, ensure_ascii=False), encoding='utf-8')
    log(f'rendered {mp4} ({dur:.1f}s)')
    if not qpath and not args.no_upload:
        remember(hist, sc)

    if mode == 'off':
        log('upload skipped (mode off)'); return
    try:
        vid = upload.upload(mp4, title, desc, tags, mode)
    except upload.QuotaError as e:
        enqueue(sc, e, qpath); gh_annotation('warning', 'YouTube quota reached, video queued for the next run.'); return
    except Exception as e:
        traceback.print_exc(); enqueue(sc, e, qpath); gh_annotation('error', f'upload failed: {e}'); raise SystemExit(1)
    record(sc, vid, mode)
    if qpath:
        qpath.unlink(missing_ok=True)
    url = f'https://youtube.com/shorts/{vid}' if fmt == 'short' else f'https://youtu.be/{vid}'
    log(f'uploaded {url} ({mode})')
    if fmt == 'short' and mode == 'public':
        social(mp4, title, url, sc)
    pls = json.loads((HERE / 'playlists.json').read_text(encoding='utf-8')) if (HERE / 'playlists.json').exists() else {}
    if sc.get('template') in pls:
        try:
            upload.add_to_playlist(pls[sc['template']], vid); log(f"added to playlist {sc['template']}")
        except Exception as e:
            log(f'playlist add skipped: {str(e)[:160]}')
    if fmt == 'long':
        try:
            upload.set_thumbnail(vid, out / 'thumb.png'); log('thumbnail set')
        except Exception as e:
            log(f'thumbnail skipped: {str(e)[:160]}')


if __name__ == '__main__':
    main()
