#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ייבוא שיעורים מיוטיוב למאגר.

מה הסקריפט עושה:
1. קורא מ-Firestore את מקורות היוטיוב הפעילים (ytSources) ואת השיעורים שממתינים לייבוא (src.type == "ytimport").
2. מוריד רק את השמע (yt-dlp), מכווץ ל-MP3 מונו 48kbps (ffmpeg) ושומר בתיקיית audio/ במאגר.
3. עושה commit + push למאגר, ורק אחר כך יוצר/מעדכן את השיעורים ב-Firestore.
4. כותב סטטוס ל-system/ytImport כדי שיופיע בדף הניהול.

הרצה:  python tools/yt_import.py            (ב-GitHub Actions או במחשב)
משתני סביבה:
  FIREBASE_SERVICE_ACCOUNT  — תוכן קובץ ה-JSON של חשבון השירות (או)
  GOOGLE_APPLICATION_CREDENTIALS — נתיב לקובץ ה-JSON (במחשב מקומי)
  PAGES_BASE   — כתובת האתר, למשל https://aryeminkov-maker.github.io/tora/
  MAX_PER_RUN  — כמה שיעורים לייבא לכל היותר בריצה (ברירת מחדל 8)
  YT_COOKIES   — (לא חובה) נתיב לקובץ cookies.txt אם יוטיוב חוסם
  DRY_RUN=1    — רק להדפיס מה היה מיובא, בלי להוריד ובלי לכתוב
"""
import json, os, re, subprocess, sys, time, random, string, datetime, traceback

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
AUDIO_DIR = os.path.join(ROOT, 'audio')
PAGES_BASE = os.environ.get('PAGES_BASE', 'https://aryeminkov-maker.github.io/tora/').rstrip('/') + '/'
MAX_PER_RUN = int(os.environ.get('MAX_PER_RUN', '8'))
MAX_MB = 95
DRY = os.environ.get('DRY_RUN') == '1'
COOKIES = os.environ.get('YT_COOKIES') or None


def log(*a):
    print(*a, flush=True)


def uid(prefix):
    return prefix + ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(7))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------- Firestore ----------------
def init_db():
    import firebase_admin
    from firebase_admin import credentials, firestore
    raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if raw:
        cred = credentials.Certificate(json.loads(raw))
    else:
        path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or os.path.join(ROOT, 'service-account.json')
        cred = credentials.Certificate(path)
    firebase_admin.initialize_app(cred)
    return firestore.client()


# ---------------- YouTube ----------------
def ydl_opts(extra=None):
    o = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'ignoreerrors': False}
    if COOKIES:
        o['cookiefile'] = COOKIES
    if extra:
        o.update(extra)
    return o


def normalize_source_url(url):
    """ערוץ (@handle) → רשימת הסרטונים של הערוץ."""
    if 'list=' in url:
        return url
    m = re.match(r'(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))', url)
    if m:
        return m.group(1) + '/videos'
    return url


def list_videos(url, limit):
    """מחזיר רשימת מזהי סרטונים, מהחדש לישן."""
    import yt_dlp
    with yt_dlp.YoutubeDL(ydl_opts({'extract_flat': 'in_playlist', 'playlistend': limit})) as ydl:
        info = ydl.extract_info(normalize_source_url(url), download=False)
    ids = []
    for e in (info or {}).get('entries') or []:
        if e and e.get('id') and len(e['id']) == 11:
            ids.append(e['id'])
    return ids


def download_audio(video_id, lesson_id):
    """מוריד ומכווץ. מחזיר (נתיב, מטא-דאטה)."""
    import yt_dlp
    os.makedirs(AUDIO_DIR, exist_ok=True)
    out_tmpl = os.path.join(AUDIO_DIR, lesson_id + '.%(ext)s')
    opts = ydl_opts({
        'format': 'bestaudio/best',
        'outtmpl': out_tmpl,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'postprocessor_args': {'extractaudio': ['-ac', '1', '-ar', '22050', '-b:a', '48k']},
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=True)
    path = os.path.join(AUDIO_DIR, lesson_id + '.mp3')
    if not os.path.exists(path):
        raise RuntimeError('קובץ השמע לא נוצר')
    size_mb = os.path.getsize(path) / 1048576
    if size_mb > MAX_MB:
        os.remove(path)
        raise RuntimeError(f'הקובץ גדול מדי ({size_mb:.0f}MB)')
    d = info.get('upload_date') or ''
    date = f'{d[:4]}-{d[4:6]}-{d[6:8]}' if len(d) == 8 else datetime.date.today().isoformat()
    meta = {
        'title': (info.get('title') or '').strip(),
        'duration': int(info.get('duration') or 0),
        'date': date,
        'desc': (info.get('description') or '').strip()[:600],
        'size': os.path.getsize(path),
    }
    return path, meta


def is_bot_block(err):
    t = str(err).lower()
    return 'sign in to confirm' in t or 'not a bot' in t or 'http error 429' in t


# ---------------- git ----------------
def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def commit_and_push(paths, n):
    if not paths:
        return
    git('add', *[os.path.relpath(p, ROOT) for p in paths])
    try:
        git('-c', 'user.name=youtube-import', '-c', 'user.email=youtube-import@users.noreply.github.com',
            'commit', '-m', f'ייבוא {n} שיעורים מיוטיוב')
    except subprocess.CalledProcessError:
        return
    for attempt in range(3):
        try:
            git('pull', '--rebase', '--quiet')
            git('push', '--quiet')
            return
        except subprocess.CalledProcessError as e:
            log('push נכשל, מנסה שוב…', e.stderr)
            time.sleep(5)
    raise RuntimeError('לא הצלחתי לדחוף את הקבצים למאגר')


# ---------------- main ----------------
def main():
    db = None if DRY and not os.environ.get('FIREBASE_SERVICE_ACCOUNT') else init_db()
    status = {'lastRun': now_iso(), 'ok': True, 'imported': 0, 'message': ''}
    jobs = []        # (kind, payload)
    blocked = False

    lessons_ref = db.collection('lessons') if db else None
    known = set()
    series_max = {}
    if db:
        for d in lessons_ref.stream():
            x = d.to_dict()
            if x.get('ytOrigin'):
                known.add(x['ytOrigin'])
            src = x.get('src') or {}
            if src.get('ytId') and src.get('type') in ('youtube', 'ytimport'):
                known.add(src['ytId'])
            if x.get('series'):
                series_max[x['series']] = max(series_max.get(x['series'], 0), int(x.get('num') or 0))
            if src.get('type') == 'ytimport' and src.get('ytId'):
                jobs.append(('queued', {'doc': d.id, 'data': x, 'ytId': src['ytId']}))

    # ---- מקורות במעקב ----
    sources = [] if not db else [(d.id, d.to_dict()) for d in db.collection('ytSources').stream()]
    for sid, src in sources:
        if not src.get('enabled'):
            continue
        try:
            first = not src.get('initialized')
            limit = int(src.get('maxExisting') or 20) if (first and src.get('importExisting')) else 30
            ids = list_videos(src['url'], max(limit, 30))
            seen = set(src.get('seen') or [])
            if first and not src.get('importExisting'):
                # בפעם הראשונה: רק לזכור את מה שכבר קיים, לא לייבא
                db.collection('ytSources').document(sid).update({'seen': ids, 'initialized': True, 'lastError': '', 'lastCheck': now_iso()})
                log(f'מקור {src.get("label")}: אותחל, {len(ids)} סרטונים קיימים סומנו כנראו')
                continue
            new_ids = [v for v in (ids[:limit] if first else ids) if v not in seen and v not in known]
            new_ids.reverse()  # מהישן לחדש, כדי שהמספור בסדרה יהיה לפי הסדר
            for v in new_ids:
                jobs.append(('source', {'sid': sid, 'src': src, 'ytId': v}))
            db.collection('ytSources').document(sid).update({'initialized': True, 'lastError': '', 'lastCheck': now_iso()})
        except Exception as e:
            msg = 'יוטיוב חוסם את השרת (נדרשת הרצה מהמחשב או cookies)' if is_bot_block(e) else str(e)[:300]
            blocked = blocked or is_bot_block(e)
            log('שגיאה במקור', src.get('url'), msg)
            if db:
                db.collection('ytSources').document(sid).update({'lastError': msg, 'lastCheck': now_iso()})

    if DRY:
        for k, p in jobs:
            log('DRY:', k, p.get('ytId'))
        return

    jobs = jobs[:MAX_PER_RUN]
    done = []   # (kind, payload, lesson_id, path, meta)
    for kind, p in jobs:
        lesson_id = p['doc'] if kind == 'queued' else uid('l')
        try:
            log('מוריד', p['ytId'], '…')
            path, meta = download_audio(p['ytId'], lesson_id)
            done.append((kind, p, lesson_id, path, meta))
            log('  ✓', meta['title'], f"{meta['size']/1048576:.1f}MB")
        except Exception as e:
            blocked = blocked or is_bot_block(e)
            log('  ✗', p['ytId'], str(e)[:300])
            if kind == 'source':
                db.collection('ytSources').document(p['sid']).update({'lastError': str(e)[:300]})

    # קודם הקבצים למאגר, ורק אחר כך השיעורים לאתר
    commit_and_push([d[3] for d in done], len(done))

    from firebase_admin import firestore as fs
    for kind, p, lesson_id, path, meta in done:
        rel = 'audio/' + os.path.basename(path)
        src_obj = {'type': 'file', 'media': 'audio', 'url': PAGES_BASE + rel, 'ghPath': rel, 'storagePath': ''}
        if kind == 'queued':
            x = p['data']
            upd = {'src': src_obj, 'ytOrigin': p['ytId'], 'size': meta['size']}
            if meta['duration']:
                upd['duration'] = meta['duration']
            if not x.get('title') or x.get('title') == 'שיעור':
                upd['title'] = meta['title']
            if not x.get('desc'):
                upd['desc'] = meta['desc']
            lessons_ref.document(lesson_id).update(upd)
        else:
            s = p['src']
            series = s.get('series') or ''
            num = 0
            if series:
                num = series_max.get(series, 0) + 1
                series_max[series] = num
            lessons_ref.document(lesson_id).set({
                'id': lesson_id, 'title': meta['title'] or 'שיעור', 'rabbiId': s.get('rabbiId'), 'orgId': s.get('orgId'),
                'category': s.get('category') or 'halacha', 'tags': s.get('tags') or [], 'series': series, 'num': num,
                'duration': meta['duration'] or 0, 'date': meta['date'], 'src': src_obj,
                'allowDownload': bool(s.get('allowDownload', True)), 'desc': meta['desc'], 'transcript': '',
                'sourceRef': '', 'sourceText': '', 'ytOrigin': p['ytId'], 'importedFrom': p['sid'],
                'uploadedBy': 'youtube-import', 'size': meta['size'],
            })
            db.collection('ytSources').document(p['sid']).update({
                'seen': fs.ArrayUnion([p['ytId']]), 'importedCount': fs.Increment(1), 'lastImport': now_iso()})
        status['imported'] += 1

    failed = len(jobs) - len(done)
    if blocked:
        status['ok'] = False
        status['message'] = 'יוטיוב חסם חלק מההורדות מהשרת. אפשר להריץ את הייבוא מהמחשב (ראו הוראות)'
    elif failed:
        status['ok'] = False
        status['message'] = f'{failed} שיעורים לא יובאו (פרטים ביומן הריצה)'
    left = max(0, len([1 for _ in jobs]) - MAX_PER_RUN)
    if not status['message'] and len(jobs) == MAX_PER_RUN:
        status['message'] = 'ייתכן שנשארו עוד שיעורים — ימשיכו בריצה הבאה'
    db.collection('system').document('ytImport').set(status)
    log('סיום:', status)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        try:
            from firebase_admin import firestore
            firestore.client().collection('system').document('ytImport').set(
                {'lastRun': now_iso(), 'ok': False, 'imported': 0, 'message': str(e)[:300]})
        except Exception:
            pass
        sys.exit(1)
