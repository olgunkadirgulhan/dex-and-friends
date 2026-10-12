"""Senaryo JSON -> MP4.

1. Her replik için ses (Kokoro) -> zaman çizelgesi (replik, aksiyon, SFX, sahne kartı)
2. Ses mix (konuşma + SFX + ducking'li müzik)
3. Kare kare cairo çizim -> ffmpeg (H.264 + AAC)
"""
import math
import random
import subprocess
import zlib
from pathlib import Path

import cairo
import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

from . import audio, backgrounds, rig
from .cast import CAST

FPS = 30
ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / 'fonts' / 'LuckiestGuy-Regular.ttf')

FORMATS = {
    'short': dict(W=1080, H=1920, ground=1400, s=8.0, sub_y=1590, font=78, hook=True,
                  xs={1: [0.5], 2: [0.27, 0.73], 3: [0.19, 0.5, 0.81]}),
    'long': dict(W=1920, H=1080, ground=905, s=6.3, sub_y=985, font=60, hook=False,
                 xs={1: [0.5], 2: [0.33, 0.67], 3: [0.24, 0.5, 0.76]}),
}

# aksiyon: (süre, [(t, sfx)], darbe anı)
ACTIONS = {
    'throw_slipper': (1.0, [(0.22, 'whoosh'), (0.62, 'slap')], 0.62),
    'slap': (0.85, [(0.32, 'slap')], 0.32),
    'faint': (1.0, [(0.55, 'thud')], 0.55),
    'run_away': (0.9, [(0.05, 'whoosh')], None),
    'jump': (0.75, [(0.02, 'boing')], None),
    'door_slam': (0.6, [(0.0, 'door')], 0.0),
    'dramatic_zoom': (1.5, [(0.0, 'sting')], None),
    'rimshot': (1.0, [(0.0, 'rimshot')], None),
    'spin': (0.8, [(0.0, 'whoosh')], None),
}
IMPACT_WORD = {'slap': 'SLAP!', 'thud': 'THUD!', 'door': 'BAM!', 'crash': 'CRASH!', 'boing': 'BOING!', 'pop': 'POP!'}


def log(m):
    print(f'[render] {m}', flush=True)


# ------------------------------------------------------------------ yardımcılar

def pil_to_surface(img):
    a = np.asarray(img.convert('RGBA')).astype(np.float32)
    alpha = a[:, :, 3:4] / 255.0
    rgb = a[:, :, :3] * alpha
    out = np.empty(a.shape, np.uint8)
    out[:, :, 0] = rgb[:, :, 2]; out[:, :, 1] = rgb[:, :, 1]; out[:, :, 2] = rgb[:, :, 0]
    out[:, :, 3] = a[:, :, 3]
    h, w = out.shape[:2]
    return cairo.ImageSurface.create_for_data(memoryview(np.ascontiguousarray(out)), cairo.FORMAT_ARGB32, w, h, w * 4)


def wrap(words, font, maxw, draw):
    lines, cur = [], []
    for i, w in enumerate(words):
        test = ' '.join(x for _, x in cur + [(i, w)])
        if cur and draw.textlength(test, font=font) > maxw:
            lines.append(cur); cur = []
        cur.append((i, w))
    if cur:
        lines.append(cur)
    return lines


def text_block(words, size, maxw, hi=None, hi_color=(255, 214, 0), color=(255, 255, 255), stroke=(0, 0, 0),
               stroke_w=None, bg=None, pad=0):
    font = ImageFont.truetype(FONT, size)
    tmp = ImageDraw.Draw(Image.new('RGBA', (8, 8)))
    rows = wrap(words, font, maxw, tmp)
    sw = stroke_w if stroke_w is not None else max(3, size // 10)
    lh = int(size * 1.12)
    widths = [tmp.textlength(' '.join(w for _, w in r), font=font) for r in rows]
    W = int(max(widths) + 2 * sw + 2 * pad + 10)
    H = int(lh * len(rows) + 2 * sw + 2 * pad + size * 0.25)
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if bg:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=int(size * 0.45), fill=bg, outline=(20, 20, 24), width=5)
    space = tmp.textlength(' ', font=font)
    for r, row in enumerate(rows):
        x = (W - widths[r]) / 2
        y = pad + sw + r * lh
        for i, w in row:
            col = hi_color if i == hi else color
            d.text((x, y), w, font=font, fill=col, stroke_width=sw, stroke_fill=stroke)
            x += tmp.textlength(w, font=font) + space
    return img


def stable(x):
    return zlib.crc32(str(x).encode())


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def lerp(a, b, k):
    return a + (b - a) * k


def lerp2(a, b, k):
    return (lerp(a[0], b[0], k), lerp(a[1], b[1], k))


# ------------------------------------------------------------------ zaman çizelgesi

def build_timeline(sc, fmt, workdir, speed_mul=1.0):
    t = 0.25
    voices, effects, mask = [], [], [(0, 0)]
    scenes = []
    for si, scene in enumerate(sc['scenes']):
        S = dict(scene, start=t, lines=[])
        if si > 0:
            effects.append((max(0, t - 0.05), audio.sfx('whoosh', si), 0.35))
        if scene.get('card'):
            S['card_span'] = (t, t + 1.3)
            t += 1.3
        for li, line in enumerate(scene['lines']):
            L = dict(line, start=t)
            text = (line.get('text') or '').strip()
            if text:
                samples, words = audio.speak(line['char'], text, line.get('emotion', 'neutral'), speed_mul)
                L['speech'] = (t + 0.05, t + 0.05 + len(samples) / audio.SR)
                L['words'] = words
                hop = audio.SR // FPS
                n = len(samples) // hop + 1
                env = np.array([np.sqrt(np.mean(samples[i * hop:(i + 1) * hop] ** 2)) if i * hop < len(samples) else 0
                                for i in range(n)])
                L['env'] = env / (env.max() + 1e-6)
                voices.append((L['speech'][0], samples, 1.0))
                mask += [(L['speech'][0] - 0.05, 0), (L['speech'][0], 1), (L['speech'][1], 1), (L['speech'][1] + 0.05, 0)]
                t = L['speech'][1]
            act = line.get('action')
            if act in ACTIONS:
                dur, snds, impact = ACTIONS[act]
                a0 = t + 0.05
                L['act'] = (a0, a0 + dur, impact)
                for dt, name in snds:
                    effects.append((a0 + dt, audio.sfx(name, li), 0.9))
                    if name in IMPACT_WORD and impact is not None and abs(dt - impact) < 1e-6:
                        L['impact_word'] = (a0 + dt, IMPACT_WORD[name])
                t = a0 + dur
            if line.get('sfx') and line['sfx'] in audio_sfx_names():
                at = L['speech'][1] - 0.1 if 'speech' in L else t
                effects.append((max(0, at), audio.sfx(line['sfx'], li + 7), 0.85))
                if line['sfx'] in IMPACT_WORD and 'impact_word' not in L:
                    L['impact_word'] = (at, IMPACT_WORD[line['sfx']])
                    L.setdefault('shake_at', at)
                if 'speech' not in L and 'act' not in L:
                    t += 0.6
            t += 0.38 if si < len(sc['scenes']) - 1 or li < len(scene['lines']) - 1 else 0.8
            L['end'] = t
            S['lines'].append(L)
        S['end'] = t
        scenes.append(S)
    total = t + 0.3
    mt = np.array([m[0] for m in mask] + [total]); mv = np.array([m[1] for m in mask] + [0])
    order = np.argsort(mt, kind='stable')
    stereo = audio.mix(total, voices, effects, audio.music(total + 1, seed=stable(sc['id']) % 10_000),
                       (mt[order], mv[order]))
    wav = Path(workdir) / 'audio.wav'
    sf.write(str(wav), stereo, audio.SR)
    return scenes, total, wav


def audio_sfx_names():
    from .cast import SFX
    return SFX


# ------------------------------------------------------------------ aktör

class Actor:
    def __init__(self, cid, x, facing, pose, emotion, seed):
        self.cid, self.x, self.facing = cid, x, facing
        self.pose, self.emotion = pose or 'standing', emotion or 'neutral'
        tg = rig.pose_targets(self.pose)
        self.cur = {k: tg[k] for k in ('hf', 'hb', 'ff', 'fb')}
        self.cur['drop'] = tg['drop']
        self.rng = random.Random(seed)
        self.next_blink = self.rng.uniform(0.5, 3)
        self.dizzy = self.gone = self.fallen = False
        self.hit_at = None
        self.gone_at = None

    def targets(self, t, action=None):
        tg = rig.pose_targets(self.pose)
        extra = {}
        if action:
            name, k, dur = action  # k: 0..1
            if name == 'throw_slipper':
                tg = rig.pose_targets('windup' if k < 0.25 else ('swing' if k < 0.7 else self.pose))
                extra['prop'] = 'slipper' if k < 0.25 else None
            elif name == 'slap':
                tg = rig.pose_targets('windup' if k < 0.3 else ('swing' if k < 0.65 else self.pose))
            elif name in ('jump',):
                tg = rig.pose_targets('celebrate')
            elif name == 'run_away':
                ph = t * 22
                tg = dict(rig.pose_targets('standing'))
                tg['ff'] = (10 * math.sin(ph) + 3, -max(0, 7 * math.cos(ph)))
                tg['fb'] = (-10 * math.sin(ph) + 3, -max(0, -7 * math.cos(ph)))
                tg['hf'] = (-10 * math.sin(ph) + 10, -40)
                tg['hb'] = (10 * math.sin(ph) - 10, -40)
        if self.fallen:
            tg = rig.pose_targets('fallen')
        return tg, extra


# ------------------------------------------------------------------ ana render

class Renderer:
    def __init__(self, sc, fmt, scenes, total, seed):
        self.sc, self.F, self.scenes, self.total = sc, FORMATS[fmt], scenes, total
        self.fmt = fmt
        self.W, self.H = self.F['W'], self.F['H']
        self.rng = random.Random(seed)
        self.bg_cache, self.sub_cache = {}, {}
        self.surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        self.ctx = cairo.Context(self.surf)
        self.hook = None
        if self.F['hook'] and sc.get('hook'):
            self.hook = pil_to_surface(text_block(sc['hook'].upper().split(), 74, self.W * 0.8, color=(20, 20, 24),
                                                  stroke_w=0, bg=(255, 255, 255, 245), pad=22))
        # sahne başına aktörler + her replik için kamera planı
        for si, S in enumerate(scenes):
            chars = S['characters']
            xs = self.F['xs'][min(3, len(chars))]
            order = {'left': 0, 'center': 1 if len(chars) == 3 else 0, 'right': len(xs) - 1}
            used, S['actors'] = set(), {}
            for i, ch in enumerate(chars):
                slot = order.get(ch.get('pos'), i)
                if slot in used or slot >= len(xs):
                    slot = next(k for k in range(len(xs)) if k not in used)
                used.add(slot)
                x = xs[slot] * self.W
                facing = 1 if xs[slot] < 0.5 else -1
                S['actors'][ch['id']] = Actor(ch['id'], x, facing, ch.get('pose'), ch.get('emotion'), seed + si * 31 + i)
            S['bg_name'] = S.get('bg') if S.get('bg') in backgrounds.NAMES else 'living_room'
            for L in S['lines']:
                L['shot'] = self.plan_shot(S, L)

    # ---- kamera
    def plan_shot(self, S, L):
        a = S['actors'].get(L.get('char'))
        emo = L.get('emotion', 'neutral')
        r = self.rng.random()
        if L.get('action') == 'dramatic_zoom' and a:
            return ('zoom_face', L['char'])
        if L.get('action') in ('throw_slipper', 'slap', 'faint', 'run_away', 'jump', 'spin', 'door_slam'):
            return ('wide', None)
        if a is None or len(S['actors']) == 1:
            return ('wide', None) if r < 0.5 else ('push', L.get('char'))
        if emo in ('angry', 'shock', 'excited') and r < 0.55:
            return ('punch', L['char'])
        if r < 0.45:
            return ('close', L['char'])
        if r < 0.6:
            return ('push', L['char'])
        return ('wide', None)

    def camera(self, S, L, t):
        W, H, s = self.W, self.H, self.F['s']
        ground = self.F['ground']
        cx, cy, z = W / 2, H / 2, 1.0
        if L is None:
            return cx, cy, 1.0
        kind, who = L['shot']
        k = (t - L['start']) / max(0.1, L['end'] - L['start'])
        a = S['actors'].get(who) if who else None
        if a:
            hx = a.x + a.offset_x if hasattr(a, 'offset_x') else a.x
            head_y = ground - 70 * s * CAST[a.cid]['scale']
        if kind == 'close' and a:
            z = 1.45 + 0.04 * k
            cx, cy = hx, head_y + (0.1 * H if self.fmt == 'short' else 0.05 * H)
        elif kind == 'punch' and a:
            p = ease((t - L['start']) / 0.18)
            z = lerp(1.1, 1.6, p) + 0.03 * k
            cx, cy = lerp(W / 2, hx, p), lerp(H / 2, head_y + 0.06 * H, p)
        elif kind == 'push' and a:
            z = 1.08 + 0.12 * ease(k)
            cx, cy = lerp(W / 2, hx, 0.35), H / 2 + 0.02 * H
        elif kind == 'zoom_face' and a:
            p = ease((t - L['start']) / 0.5)
            z = lerp(1.0, 2.6, p)
            cx, cy = lerp(W / 2, hx, p), lerp(H / 2, head_y, p)
        else:
            z = 1.0 + 0.03 * k
        # kadrajı görüntü içinde tut
        hw, hh = W / (2 * z), H / (2 * z)
        cx = min(max(cx, hw), W - hw)
        cy = min(max(cy, hh), H - hh)
        return cx, cy, z

    # ---- yardımcı
    def bg(self, S, si):
        key = (S['bg_name'], si)
        if key not in self.bg_cache:
            self.bg_cache = {key: backgrounds.render(S['bg_name'], self.W, self.H, self.F['ground'],
                                                     seed=stable((self.sc['id'], si)) % 1000)}
        return self.bg_cache[key]

    def subtitle(self, key, L, wi):
        ck = (key, wi)
        if ck not in self.sub_cache:
            if len(self.sub_cache) > 400:
                self.sub_cache.clear()
            words = [w['text'].upper() for w in L['words']]
            acc = tuple(int(v * 255) for v in CAST[L['char']]['accent'])
            self.sub_cache[ck] = pil_to_surface(text_block(words, self.F['font'], self.W * 0.86, hi=wi, hi_color=acc))
        return self.sub_cache[ck]

    def find(self, t):
        for si, S in enumerate(self.scenes):
            if t < S['end'] or si == len(self.scenes) - 1:
                cur = None
                for L in S['lines']:
                    if t >= L['start']:
                        cur = L
                return si, S, cur
        return 0, self.scenes[0], None

    # ---- kare
    def frame(self, t, prev_line):
        ctx, W, H, s, ground = self.ctx, self.W, self.H, self.F['s'], self.F['ground']
        si, S, L = self.find(t)
        actors = S['actors']

        # replik başlangıcında duygu/poz güncelle
        if L is not None and L is not prev_line:
            a = actors.get(L.get('char'))
            if a:
                if L.get('emotion'):
                    a.emotion = L['emotion']
                if L.get('pose'):
                    a.pose = L['pose']
            for cid, emo in (L.get('reactions') or {}).items():
                if cid in actors:
                    actors[cid].emotion = emo

        cx, cy, z = self.camera(S, L, t)
        shake = 0.0
        for LL in S['lines']:
            for key in ('impact_word', ):
                if key in LL and 0 <= t - LL[key][0] < 0.4:
                    shake = max(shake, 1 - (t - LL[key][0]) / 0.4)
            if LL.get('action') == 'door_slam' and 'act' in LL and 0 <= t - LL['act'][0] < 0.4:
                shake = max(shake, 1 - (t - LL['act'][0]) / 0.4)
        sx = (math.sin(t * 91) * 22 + math.sin(t * 57) * 10) * shake
        sy = (math.cos(t * 77) * 22) * shake

        ctx.save()
        ctx.translate(W / 2 + sx, H / 2 + sy)
        ctx.scale(z, z)
        ctx.translate(-cx, -cy)
        ctx.set_source_surface(self.bg(S, si), 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_BILINEAR)
        ctx.paint()

        # aktif aksiyon
        act_line = None
        if L is not None and 'act' in L and L['act'][0] <= t < L['act'][1] + 0.001:
            act_line = L

        # hedef (karşıdaki karakter)
        def target_of(cid):
            if L and L.get('target') in actors and L.get('target') != cid:
                return actors[L['target']]
            others = [a for k, a in actors.items() if k != cid and not a.gone]
            if not others:
                return None
            me = actors[cid]
            return min(others, key=lambda o: abs(o.x - me.x))

        projectiles = []
        for cid, a in actors.items():
            a.offset_x = getattr(a, 'offset_x', 0.0)
            action = None
            speaking = L is not None and L.get('char') == cid and 'speech' in L and L['speech'][0] <= t < L['speech'][1]
            if act_line is not None and act_line.get('char') == cid:
                a0, a1, imp = act_line['act']
                action = (act_line['action'], (t - a0) / (a1 - a0), a1 - a0)
            tg, extra = a.targets(t, action)

            lift, tilt, spin, fall = 0.0, 0.0, 1.0, 0.0
            if action:
                name, k, dur = action
                tgt = target_of(cid)
                if name == 'jump':
                    lift = math.sin(math.pi * min(1, k * 1.1)) * 26
                elif name == 'faint' and k >= 0.2:
                    fall = ease((k - 0.2) / 0.35)
                    if k > 0.55:
                        a.fallen = a.dizzy = True
                elif name == 'run_away':
                    a.offset_x = -a.facing * (k ** 1.6) * W * 0.9
                    if k > 0.98:
                        a.gone = True
                elif name == 'spin':
                    spin = math.cos(k * 4 * math.pi)
                elif name == 'slap' and tgt:
                    reach = (abs(tgt.x - a.x) - 26 * s) * (1 if tgt.x > a.x else -1)
                    p = ease(k / 0.25) if k < 0.6 else 1 - ease((k - 0.6) / 0.4)
                    a.offset_x = reach * p
                    if k >= 0.38 and tgt.hit_at is None:
                        tgt.hit_at = t; tgt.dizzy = True; tgt.emotion = 'shock'
                elif name == 'throw_slipper' and tgt:
                    if 0.25 <= k < 0.62:
                        p = (k - 0.25) / 0.37
                        hx0, hy0 = a.x + a.facing * 20 * s, ground - 75 * s
                        hx1, hy1 = tgt.x, ground - 78 * s * CAST[tgt.cid]['scale']
                        projectiles.append(('slipper', lerp(hx0, hx1, p), lerp(hy0, hy1, p) - math.sin(math.pi * p) * 90,
                                            p * 14 * a.facing))
                    if k >= 0.62 and tgt.hit_at is None:
                        tgt.hit_at = t; tgt.dizzy = True; tgt.emotion = 'shock'
            if a.fallen:
                fall = 1.0
            if a.gone:
                continue

            # yumuşatılmış poz
            kk = 0.45 if action else 0.3
            for key in ('hf', 'hb', 'ff', 'fb'):
                a.cur[key] = lerp2(a.cur[key], tg[key], kk)
            a.cur['drop'] = lerp(a.cur['drop'], tg['drop'], kk)

            # göz kırpma
            blink = 0
            if t >= a.next_blink:
                blink = 1
                if t >= a.next_blink + 0.12:
                    a.next_blink = t + a.rng.uniform(2.5, 5)
            # ağız
            mouth, bob = 'closed', 0.0
            if speaking:
                i = int((t - L['speech'][0]) * FPS)
                lvl = L['env'][min(i, len(L['env']) - 1)]
                bob = -lvl * 1.5
                mouth = mouth_shape(lvl, L, t - L['speech'][0], a.emotion)
            # darbe geri tepmesi
            head_tilt = 0.0
            if a.hit_at is not None and t - a.hit_at < 0.6:
                d = t - a.hit_at
                head_tilt = -25 * math.exp(-d * 6) * math.cos(d * 25)
                tilt = tilt + -8 * math.exp(-d * 5)
            elif speaking:
                head_tilt = 2.5 * math.sin(t * 7)

            st = dict(a.cur)
            st.update(emotion=a.emotion, mouth=mouth, blink=blink, t=t, bob=bob, head_tilt=head_tilt,
                      breath=1 + 0.012 * math.sin(t * math.pi + stable(cid) % 7), tilt=tilt, lift=lift,
                      dizzy=a.dizzy, fall=fall, seat=tg.get('seat'), bend=tg.get('bend', 'out'),
                      prop=extra.get('prop', tg.get('prop')))
            if a.pose == 'fist' and a.cid == 'mama_rose' and not action:
                st['prop'] = 'slipper'
            ctx.save()
            if spin != 1.0:
                ctx.translate(a.x + a.offset_x, 0); ctx.scale(spin if abs(spin) > 0.05 else 0.05, 1)
                ctx.translate(-(a.x + a.offset_x), 0)
            rig.draw(ctx, cid, a.x + a.offset_x, ground, s, a.facing, st)
            ctx.restore()
            if action and action[0] == 'run_away':
                speed_lines(ctx, a.x + a.offset_x, ground - 50 * s, s, a.facing, t)

        for kind, x, y, rot in projectiles:
            rig.slipper(ctx, x, y, s * 1.3, rot)

        # darbe patlaması
        for LL in S['lines']:
            iw = LL.get('impact_word')
            if iw and 0 <= t - iw[0] < 0.45:
                tgt = actors.get(LL.get('target')) if LL.get('target') in actors else None
                if tgt is None:
                    tgt = next((a for k, a in actors.items() if k != LL.get('char')), None)
                x, y = (tgt.x if tgt else W / 2), ground - 80 * s
                me = actors.get(LL.get('char'))
                if LL.get('action') == 'faint' and me:
                    x, y = me.x + me.facing * 30 * s, ground - 25 * s
                elif LL.get('action') == 'door_slam':
                    x = W / 2
                burst(ctx, x, y, s, iw[1], (t - iw[0]) / 0.45)
        ctx.restore()

        # ekran-uzayı katmanları
        if self.hook is not None:
            ctx.set_source_surface(self.hook, (W - self.hook.get_width()) / 2, 150)
            ctx.paint()
        if S.get('label'):
            label = self.sub_cache.setdefault(('label', si), pil_to_surface(
                text_block(S['label'].upper().split(), 54, W * 0.8, color=(255, 255, 255), stroke_w=0,
                           bg=(230, 40, 60, 255), pad=12)))
            ctx.set_source_surface(label, 40, 150 + (self.hook.get_height() + 30 if self.hook else 0))
            ctx.paint()
        if L is not None and 'words' in L and t < L['speech'][1] + 0.25:
            rel = t - L['speech'][0]
            wi = 0
            for i, w in enumerate(L['words']):
                if rel >= w['start']:
                    wi = i
            sub = self.subtitle(id(L), L, wi)
            pop = 0.85 + 0.15 * ease((t - L['speech'][0]) / 0.12)
            ctx.save()
            ctx.translate(W / 2, self.F['sub_y'])
            ctx.scale(pop, pop)
            ctx.set_source_surface(sub, -sub.get_width() / 2, -sub.get_height() / 2)
            ctx.paint()
            ctx.restore()
        if S.get('card_span') and S['card_span'][0] <= t < S['card_span'][1]:
            k = (t - S['card_span'][0]) / (S['card_span'][1] - S['card_span'][0])
            card(ctx, W, H, S['card'], k, self.F['font'], t)
        # sahne geçişi: kısa beyaz flaş
        if si > 0 and 0 <= t - S['start'] < 0.1:
            ctx.set_source_rgba(1, 1, 1, 1 - (t - S['start']) / 0.1)
            ctx.paint()
        self.surf.flush()
        return L


def mouth_shape(lvl, L, rel, emo):
    if lvl < 0.13:
        return 'closed'
    if lvl > 0.8 and emo in ('angry', 'shock', 'excited', 'cry'):
        return 'wide'
    word = L['words'][0]
    for w in L['words']:
        if rel >= w['start']:
            word = w
    letters = [c for c in word['text'].lower() if c.isalpha()] or ['a']
    p = (rel - word['start']) / max(0.05, word['end'] - word['start'])
    ch = letters[min(len(letters) - 1, int(p * len(letters)))]
    vow = [c for c in letters if c in 'aeiouy']
    if ch not in 'aeiouwy' and vow:
        ch = vow[min(len(vow) - 1, int(p * len(vow)))]
    shape = {'a': 'ai', 'i': 'ai', 'e': 'e', 'y': 'e', 'o': 'o', 'u': 'u', 'w': 'u'}.get(ch, 'e')
    if lvl < 0.3:
        return 'e' if shape != 'u' else 'u'
    return shape


def speed_lines(ctx, x, y, s, facing, t):
    ctx.save()
    ctx.set_source_rgba(1, 1, 1, 0.85)
    ctx.set_line_width(s * 0.8)
    r = random.Random(int(t * 30))
    for i in range(7):
        yy = y + (i - 3) * s * 9 + r.uniform(-s * 2, s * 2)
        x0 = x + facing * s * r.uniform(20, 30)
        ctx.move_to(x0, yy); ctx.line_to(x0 + facing * s * r.uniform(25, 50), yy)
    ctx.stroke()
    ctx.restore()


def burst(ctx, x, y, s, word, k):
    sc = s * (0.6 + 0.6 * ease(k / 0.3)) * (1 - 0.3 * max(0, k - 0.7) / 0.3)
    ctx.save()
    ctx.translate(x, y)
    ctx.rotate(-0.15)
    ctx.new_path()
    for i in range(24):
        a = i * math.pi / 12
        r = (16 if i % 2 == 0 else 10) * sc
        (ctx.move_to if i == 0 else ctx.line_to)(r * math.cos(a) * 1.35, r * math.sin(a))
    ctx.close_path()
    ctx.set_source_rgba(1, 0.86, 0.1, 1 - max(0, k - 0.75) * 4)
    ctx.fill_preserve()
    ctx.set_source_rgb(0.1, 0.1, 0.1); ctx.set_line_width(sc * 0.8); ctx.stroke()
    ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(7.5 * sc)
    ext = ctx.text_extents(word)
    ctx.move_to(-ext.width / 2 - ext.x_bearing, ext.height / 2)
    ctx.text_path(word)
    ctx.set_source_rgb(0.9, 0.1, 0.12); ctx.fill_preserve()
    ctx.set_source_rgb(1, 1, 1); ctx.set_line_width(sc * 0.35); ctx.stroke()
    ctx.restore()


_card_cache = {}


def card(ctx, W, H, text, k, size, t):
    ctx.save()
    ctx.set_source_rgb(0.12, 0.45, 0.95); ctx.paint()
    ctx.translate(W / 2, H / 2)
    ctx.rotate(t * 0.6)
    for i in range(16):
        ctx.move_to(0, 0)
        ctx.arc(0, 0, max(W, H), i * math.pi / 8, i * math.pi / 8 + math.pi / 16)
        ctx.close_path()
    ctx.set_source_rgba(1, 1, 1, 0.12); ctx.fill()
    ctx.restore()
    key = (text, W)
    if key not in _card_cache:
        _card_cache[key] = pil_to_surface(text_block(text.upper().split(), int(size * 1.5), W * 0.8,
                                                     color=(255, 230, 40)))
    surf = _card_cache[key]
    p = 0.7 + 0.3 * ease(k / 0.2)
    ctx.save(); ctx.translate(W / 2, H / 2); ctx.scale(p, p)
    ctx.set_source_surface(surf, -surf.get_width() / 2, -surf.get_height() / 2); ctx.paint()
    ctx.restore()


# ------------------------------------------------------------------ giriş noktası

def render(sc, out_dir, preview_png=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = sc.get('format', 'short')
    F = FORMATS[fmt]
    scenes, total, wav = build_timeline(sc, fmt, out_dir)
    limit = 58.5 if fmt == 'short' else 600
    if total > limit:
        k = min(1.35, total / (limit - 1.5))
        log(f'{total:.1f}s too long, re-voicing at x{k:.2f}')
        scenes, total, wav = build_timeline(sc, fmt, out_dir, speed_mul=k)
    log(f'timeline {total:.1f}s, {sum(len(S["lines"]) for S in scenes)} lines')
    R = Renderer(sc, fmt, scenes, total, seed=stable(sc['id']) % 100000)
    mp4 = out_dir / 'video.mp4'
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgra', '-s', f"{F['W']}x{F['H']}",
           '-r', str(FPS), '-i', '-', '-i', str(wav), '-map', '0:v', '-map', '1:a',
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
           '-c:a', 'aac', '-b:a', '192k', '-shortest', str(mp4)]
    loop = fmt == 'short'   # Shorts başa sarar: son 0.45 sn ilk kareye karışır, ses yumuşak biter (kesintisiz döngü)
    if loop:
        cmd[cmd.index('-c:a'):cmd.index('-c:a')] = ['-af', f'afade=t=out:st={max(0.0, total - 0.3):.2f}:d=0.3']
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = int(total * FPS)
    prev = None
    first = None
    for i in range(n):
        t = i / FPS
        prev = R.frame(t, prev)
        buf = bytes(R.surf.get_data())
        if i == 0:
            first = np.frombuffer(buf, np.uint8).astype(np.float32)
        if loop and t > total - 0.45:
            k = min(1.0, (t - (total - 0.45)) / 0.45)
            buf = (np.frombuffer(buf, np.uint8).astype(np.float32) * (1 - k) + first * k).astype(np.uint8).tobytes()
        proc.stdin.write(buf)
        if preview_png and i == int(n * 0.3):
            R.surf.write_to_png(str(preview_png))
        if i % (FPS * 10) == 0:
            log(f'frame {i}/{n}')
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError('ffmpeg failed')
    return mp4, total
