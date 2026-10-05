"""Senaryo üretimi: Gemini yazar -> Gemini hakem (1-10) -> doğrulama. Anahtar yoksa / başarısızsa bank/ kullanılır."""
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import backgrounds
from .cast import ACTIONS, CAST, EMOTIONS, POSES, SFX, bible

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / 'prompts'
BANK = ROOT / 'bank'
# flash modelleri aynı kapasite havuzunda, çoğu zaman birlikte 503 veriyor; lite ve gemma genelde yanıt verir
MODELS = ('gemini-3.8-flash,gemini-3.5-flash,gemini-flash-latest,gemini-3.7-flash,gemini-3-flash-preview,'
          'gemini-flash-lite-latest,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-26b-a4b-it,gemma-4-31b-it')

SHORT_TEMPLATES = {
    'caught': 'Caught: Dex lies -> Nia or Mama Rose catches him with evidence -> punishment punchline.',
    'expectation_reality': 'Expectation vs Reality: a character describes a plan (scene label "Expectation"), '
                           'then a scene labelled "Reality" shows the disaster.',
    'mom_logic': "Mom Logic: Mama Rose states an absurd house rule -> Dex argues -> the slipper.",
    'types_of_people': 'Types of People: "Types of people at X" -> 3-4 fast scenes, each a type (use scene labels '
                       'like "The Show-Off"), the last one the most absurd.',
    'plot_twist': 'Plot Twist: a normal conversation -> a totally unexpected twist in the last 3 seconds.',
    'sibling_war': 'Sibling War: Dex and Pip fight over something tiny -> it escalates absurdly -> Mama Rose '
                   'ends it in the last line.',
    'pov': 'POV: the hook starts with "POV:" and puts the viewer in a relatable everyday moment; the characters '
           'react to "you" (the viewer) and it gets worse each line.',
    'rules_of': 'Unspoken Rules: "Unspoken rules of X" -> 3 quick scenes, each a rule (scene label "Rule #1" ...), '
                'the last rule breaks the fourth wall.',
}
# Medyan izlenmeye göre ağırlık (2026-10-05: types_of_people 1389, plot_twist 1175, mom_logic 1084,
# expectation_reality 1006, caught 994); yeni şablonlar denensin diye orta ağırlıkta
TEMPLATE_WEIGHTS = {'types_of_people': 4, 'plot_twist': 3, 'mom_logic': 3, 'expectation_reality': 2, 'caught': 2,
                    'sibling_war': 3, 'pov': 2, 'rules_of': 2}
LONG_SERIES = {
    'dexs_terrible_ideas': "Dex's Terrible Ideas: one of Dex's business/life plans turns into a disaster.",
    'growing_up_with_mama_rose': 'Growing Up With Mama Rose: family memories, rules and chaos at home.',
    'big_tonys_business_lessons': "Big Tony's Business Lessons: Big Tony 'teaches' business and it backfires.",
}
TOPICS = ['school', 'parents rules', 'sibling fight', 'first job', 'paying rent', 'going to the doctor', 'diet',
          'the gym', 'meeting the girlfriend\'s parents', 'a wedding', 'vacation', 'phone addiction',
          'the night before an exam', 'the neighbor', 'holiday dinner', 'grocery shopping', 'traffic',
          'a job interview', 'cleaning the room', 'a birthday party', 'online shopping', 'a school trip',
          'learning to drive', 'the wifi goes down', 'a haircut', 'a surprise party', 'babysitting Pip',
          'a restaurant bill', 'a report card', 'moving to a new house', 'a video game', 'a pet goldfish',
          'the last slice of pizza', 'group projects', 'the TV remote', 'a power outage', 'waking up late',
          'a family road trip', 'the dentist', 'picture day at school', 'a snow day', 'sharing a bedroom',
          'the school cafeteria', 'a garage sale', 'a science fair', 'a fire drill', 'grandma visiting',
          'returning something to the store', 'the ice cream truck', 'a broken vase', 'a spelling bee',
          'chores day', 'the first day of school', 'a lost phone', 'a camping trip', 'a new puppy',
          'a talent show', 'the self checkout', 'a long flight', 'leftovers in the fridge']
BANNED = re.compile(r'\b(kill|dead|die|blood|sex|sexy|drunk|beer|wine|drug|damn|hell|stupid idiot|shut up)\b', re.I)


def log(m):
    print(f'[writer] {m}', flush=True)


def gemini(prompt, temperature=0.95, timeout=120, as_json=True):
    key = (os.environ.get('GEMINI_API_KEY') or '').strip().lstrip('﻿')
    if not key:
        raise RuntimeError('GEMINI_API_KEY not set')
    models = [m.strip() for m in (os.environ.get('GEMINI_MODELS') or MODELS).split(',') if m.strip()]
    errors = []
    for attempt in range(2):
        for model in models:
            cfg = {'temperature': temperature}
            if as_json and not model.startswith('gemma'):   # gemma'da JSON modu yok; parse_json çitleri temizler
                cfg['responseMimeType'] = 'application/json'
            body = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': cfg}
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
            try:
                r = requests.post(url, json=body, headers={'x-goog-api-key': key}, timeout=timeout)
            except requests.RequestException as e:
                errors.append(f'{model}:{type(e).__name__}'); continue
            if r.status_code == 200:
                cands = r.json().get('candidates') or [{}]
                parts = cands[0].get('content', {}).get('parts', [])
                text = ''.join(p.get('text', '') for p in parts if not p.get('thought')).strip()
                if text:
                    return text
                errors.append(f'{model}:empty'); continue
            errors.append(f'{model}:{r.status_code}')
            if r.status_code not in (404, 429, 500, 503):
                r.raise_for_status()
        if attempt == 0:
            time.sleep(60)   # tüm modeller meşgul: yoğunluk genelde 1-2 dakikada geçer
    raise RuntimeError(f"Gemini unavailable: {', '.join(errors)}")


def parse_json(text):
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M).strip()
    start = text.find('{')
    return json.loads(text[start: text.rfind('}') + 1])


# ------------------------------------------------------------------ doğrulama

def normalize(sc, fmt):
    """Bilinmeyen değerleri güvenli varsayılana çek; ciddi sorunları liste olarak döndür."""
    problems = []
    max_words = 12 if fmt == 'short' else 20
    sc['format'] = fmt
    if not sc.get('scenes'):
        return ['no scenes']
    n_lines = 0
    has_gag = False
    for S in sc['scenes']:
        if S.get('bg') not in backgrounds.NAMES:
            S['bg'] = 'living_room'
        chars = [c for c in S.get('characters', []) if c.get('id') in CAST][:3]
        ids = [c['id'] for c in chars]
        S['characters'] = chars
        for L in S.get('lines', []):
            if L.get('char') not in CAST:
                problems.append(f"unknown character {L.get('char')}"); continue
            if L['char'] not in ids and len(chars) < 3:
                chars.append({'id': L['char'], 'pos': 'right' if chars and chars[0].get('pos') == 'left' else 'left'})
                ids.append(L['char'])
            if L.get('emotion') not in EMOTIONS:
                L['emotion'] = 'neutral'
            if L.get('pose') and L['pose'] not in POSES:
                L['pose'] = 'standing'
            if L.get('action') and L['action'] not in ACTIONS:
                L['action'] = None
            if L.get('sfx') and L['sfx'] not in SFX:
                L['sfx'] = None
            if L.get('target') and L['target'] not in ids:
                L['target'] = None
            L['reactions'] = {k: v for k, v in (L.get('reactions') or {}).items() if k in ids and v in EMOTIONS}
            text = (L.get('text') or '').strip()
            L['text'] = text
            if len(text.split()) > max_words:
                problems.append(f'line too long ({len(text.split())} words): {text}')
            if BANNED.search(text):
                problems.append(f'not family friendly: {text}')
            if text:
                n_lines += 1
            if L.get('action') or L.get('sfx'):
                has_gag = True
        S['lines'] = [L for L in S.get('lines', []) if L.get('char') in ids and (L['text'] or L.get('action') or L.get('sfx'))]
        if not S['characters']:
            problems.append('scene without characters')
    sc['scenes'] = [S for S in sc['scenes'] if S['lines']]
    lo, hi = (8, 14) if fmt == 'short' else (30, 70)
    if not lo <= n_lines <= hi:
        problems.append(f'{n_lines} spoken lines (want {lo}-{hi})')
    if not has_gag:
        problems.append('no physical gag / sound effect')
    for k in ('title', 'hook'):
        if not sc.get(k):
            if k == 'hook' and fmt == 'long':
                continue
            problems.append(f'missing {k}')
    return problems


# ------------------------------------------------------------------ üretim

def title_key(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def published_titles():
    """Kanala yüklenmiş tüm başlıklar (kopya başlık = YouTube'da tekrar eden içerik sinyali)."""
    p = ROOT / 'published.csv'
    if not p.exists():
        return set()
    import csv
    with p.open(encoding='utf-8') as f:
        return {title_key(r.get('title')) for r in csv.DictReader(f)}


def load_hist(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'recent': [], 'bank_used': []}


def pick(hist, fmt, rnd):
    recent = [r for r in hist['recent'] if r.get('format', 'short') == fmt]
    if fmt == 'short':
        last = recent[-1]['template'] if recent else None
        options = [k for k in SHORT_TEMPLATES if k != last]
        template = rnd.choices(options, weights=[TEMPLATE_WEIGHTS.get(k, 1) for k in options])[0]
    else:
        done = [r['template'] for r in recent]
        order = list(LONG_SERIES)
        template = order[len(done) % len(order)]
    used = {r.get('topic') for r in hist['recent'][-30:]}
    topic = rnd.choice([t for t in TOPICS if t not in used] or TOPICS)
    return template, topic


def schema_text():
    return (PROMPTS / 'schema.json').read_text(encoding='utf-8')


def write_with_gemini(fmt, template, topic, hist):
    base = (PROMPTS / ('script_short.txt' if fmt == 'short' else 'script_long.txt')).read_text(encoding='utf-8')
    recent_titles = '\n'.join('- ' + r.get('title', '') for r in hist['recent'][-30:])
    prompt = base.format(
        character_bible=bible(),
        template=(SHORT_TEMPLATES if fmt == 'short' else LONG_SERIES)[template],
        topic=topic,
        schema=schema_text(),
        backgrounds=', '.join(backgrounds.NAMES),
        poses=', '.join(POSES), emotions=', '.join(EMOTIONS), actions=', '.join(ACTIONS), sfx=', '.join(SFX),
        recent=recent_titles or '(none yet)',
    )
    if fmt == 'short':
        ex = sorted(BANK.glob('*.json'))
        random.shuffle(ex)
        shots = []
        for p in ex[:2]:
            e = json.loads(p.read_text(encoding='utf-8'))
            shots.append('\n'.join(f"{L['char']}: {L['text']}" + (f"  [action: {L['action']}]" if L.get('action') else '')
                                   for S in e['scenes'] for L in S['lines']))
        prompt += ('\n\nEXAMPLES of the pacing, length (9-12 lines) and joke density we want. Every line is a joke '
                   'or a setup; excuses get more absurd; the ending flips everything. Do NOT copy these jokes or '
                   'topics:\n\n' + '\n\n---\n\n'.join(shots))
    judge_tpl = (PROMPTS / 'judge.txt').read_text(encoding='utf-8')
    best, best_score, feedback = None, -1, ''
    for attempt in range(4):
        try:
            sc = parse_json(gemini(prompt + feedback))
        except Exception as e:
            log(f'write attempt {attempt + 1} failed: {str(e)[:200]}'); continue
        problems = normalize(sc, fmt)
        if title_key(sc.get('title')) in published_titles():
            problems.append(f"title already used on the channel: {sc.get('title')}")
        if problems:
            log(f'attempt {attempt + 1} rejected: {problems[:3]}')
            feedback = '\n\nYour previous script was rejected for: ' + '; '.join(problems[:5]) + '. Fix these.'
            continue
        try:
            j = parse_json(gemini(judge_tpl + '\n\nSCRIPT:\n' + json.dumps(sc, ensure_ascii=False), temperature=0.2))
            score = float(j.get('score', 0))
        except Exception as e:
            log(f'judge failed ({str(e)[:120]}), accepting script'); score, j = 7.0, {}
        log(f'attempt {attempt + 1}: score {score} | {sc.get("title")}')
        if score > best_score:
            best, best_score = sc, score
        if score >= 7:
            break
        feedback = (f"\n\nA comedy editor rated your last script {score}/10. Weakest part: {j.get('weakest_part')}. "
                    f"Fix: {j.get('fix')}. Write a NEW, funnier script.")
    if best is None:
        raise RuntimeError('Gemini produced no valid script')
    best['judge_score'] = best_score
    return best


def from_bank(fmt, hist):
    used = set(hist.get('bank_used', []))
    for p in sorted(BANK.glob('*.json')):
        sc = json.loads(p.read_text(encoding='utf-8'))
        if sc.get('format', 'short') == fmt and sc['id'] not in used:
            normalize(sc, fmt)
            sc['source'] = 'bank'
            return sc
    return None


def make_script(fmt, hist, seed=None):
    rnd = random.Random(seed)
    template, topic = pick(hist, fmt, rnd)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    prefer_bank = os.environ.get('SCRIPT_SOURCE', '').lower() == 'bank'
    if os.environ.get('GEMINI_API_KEY') and not prefer_bank:
        try:
            sc = write_with_gemini(fmt, template, topic, hist)
            sc.update(id=f'{fmt}_{stamp}', template=template, topic=topic, source='gemini')
            if sc.get('judge_score', 0) >= 5 or fmt != 'short' or from_bank(fmt, hist) is None:
                return sc
            log(f"best Gemini score {sc['judge_score']} < 5, using a bank script instead")
        except Exception as e:
            log(f'Gemini failed, falling back to bank: {str(e)[:200]}')
    sc = from_bank(fmt, hist)
    if sc is None:
        raise RuntimeError(f'no {fmt} script available: set GEMINI_API_KEY or add scripts to bank/')
    sc['bank_id'] = sc['id']
    sc['id'] = f"{fmt}_{stamp}"
    return sc
