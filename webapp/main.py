#!/usr/bin/env python3
"""
webapp/main.py — Selbst-Service-Onboarding für den Wohnungsmonitor
==================================================================

Eine schlanke FastAPI-App, mit der eine Person mit einem Einladungscode:
  → sich per Clerk (Google) anmeldet
  → ihren Einladungscode eingibt
  → ihre Wohnungs-Suchfilter setzt (Preis, Größe, Bezirke, …)
  → Telegram per 1-Klick-Button verknüpft (/start-Code)

Sie schreibt in die SELBE SQLite (profiles.py), die der Monitor liest — ein
aktivierter Nutzer wird beim nächsten Lauf mit seinen Filtern bedacht.
Server-rendered, dependency-light (kein Template-Engine); alle Nutzerinhalte
werden HTML-escaped. UI auf Deutsch.

Lokal testen:
    ALLOW_DEV_LOGIN=1 SESSION_SECRET=dev uvicorn webapp.main:app --port 8000
Clerk-Login aktiviert sich mit CLERK_PUBLISHABLE_KEY (+ BASE_URL).
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

import profiles
import webapp.clerk_auth as clerk_auth
from webapp import ratelimit


def _load_env_file() -> None:
    """Lokale .env laden (überschreibt KEINE bereits gesetzten Env-Variablen)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k and os.environ.get(k, "") == "":
                    os.environ[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass


_load_env_file()
os.umask(0o077)  # neue Dateien 0600 / Verzeichnisse 0700 — DB nicht world-readable

BASE_URL       = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
_IS_HTTPS      = BASE_URL.startswith("https://")
_IS_LOCAL      = "localhost" in BASE_URL or "127.0.0.1" in BASE_URL
SESSION_SECRET = os.environ.get("SESSION_SECRET", "") or secrets.token_hex(16)
# Dev-Login ist ein passwortloser Bypass — niemals gegen eine öffentliche URL
# ehren, selbst wenn eine kopierte .env ihn setzt.
ALLOW_DEV_LOGIN = os.environ.get("ALLOW_DEV_LOGIN", "").strip() in ("1", "true") and _IS_LOCAL
BOT_USERNAME    = os.environ.get("BOT_USERNAME", "Noapartmentsbot")
# Admin-Zugang (Admin-Panel /admin): kommagetrennte E-Mails.
ADMIN_EMAILS    = {e.strip().lower() for e in
                   os.environ.get("ADMIN_EMAILS", "sebastianspranger699@gmail.com").split(",")
                   if e.strip()}

app = FastAPI(title="Wohnungsmonitor Onboarding")
# Sicheres Cookie: HttpOnly (Starlette-Default) + Secure auf HTTPS + SameSite=lax
# (lax, damit der Clerk-Rückweg quer durchs Netz die Session trotzdem trägt).
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   max_age=60 * 60 * 24 * 7, https_only=_IS_HTTPS, same_site="lax")


# ── kleine Helfer ──────────────────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s or ""))


def _auth(request: Request):
    """(uid, email) aus der Session oder None."""
    email = (request.session.get("email") or "").strip()
    uid = (request.session.get("uid") or "").strip()
    if not email or not uid:
        return None
    return uid, email


def _require(request: Request):
    auth = _auth(request)
    if not auth:
        raise HTTPException(303, headers={"Location": "/"})
    return auth


def _is_admin(email: str) -> bool:
    return (email or "").strip().lower() in ADMIN_EMAILS


def _require_admin(request: Request):
    """Nur Admins: sonst 403 (kein Umweg über Redirect, damit nicht verraten
    wird, dass das Admin-Panel existiert)."""
    uid, email = _require(request)
    if not _is_admin(email):
        raise HTTPException(403, "Kein Admin-Zugang")
    return uid, email


def _num(v: str):
    """Deutsche Zahlen ('1.600,50' / '1600') → float | None."""
    s = (v or "").strip().replace(" ", "").replace(".", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_filters(values: dict) -> dict:
    """Formularwerte (dict) → Filter-Dict (nur gesetzte Werte, bereinigt)."""
    f = {}
    wm = _num(values.get("max_warm_miete", ""))
    km = _num(values.get("max_kalt_miete", ""))
    gr = _num(values.get("min_groesse", ""))
    zi = _num(values.get("min_zimmer", ""))
    rd = _num(values.get("max_radius_km", ""))
    if wm is not None and wm >= 0:
        f["max_warm_miete"] = round(wm, 0)
    if km is not None and km >= 0:
        f["max_kalt_miete"] = round(km, 0)
    if gr is not None and gr >= 0:
        f["min_groesse"] = round(gr, 0)
    if zi is not None and zi >= 0:
        f["min_zimmer"] = round(zi, 1)
    if rd is not None and rd > 0:
        f["max_radius_km"] = round(min(rd, 50.0), 1)
    bez = [b.strip().lower() for b in
           (values.get("bezirke_erlaubt", "") or "").split(",")]
    bez = list(dict.fromkeys(b for b in bez if b))
    if bez:
        f["bezirke_erlaubt"] = bez
    return f


def _fmt(v, fmt: str = ".0f") -> str:
    return f"{v:{fmt}}" if v is not None and v != "" else ""


# ── CSS + Layout ───────────────────────────────────────────────────────────

_CSS = """\
:root{--accent:#1d4ed8;--accent-d:#1e40af;--bg:#f6f8fb;--card:#fff;--text:#111827;--muted:#6b7280}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);color:var(--text)}
main{max-width:640px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:0 0 6px}
h2{font-size:1.05rem;margin:0 0 10px}
p{margin:8px 0}
.muted{color:var(--muted);font-size:.9rem}
.hero{padding:18px 0 10px}
.card{background:var(--card);border:1px solid #e5e7eb;border-radius:12px;
      padding:16px 18px;margin:14px 0}
label{display:block;font-size:.85rem;font-weight:600;margin:12px 0 4px}
input,select{width:100%;padding:9px 11px;border:1px solid #d1d5db;border-radius:8px;
             font-size:1rem;background:#fff}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
button,.btn{display:inline-block;background:var(--accent);color:#fff;border:0;
            border-radius:9px;padding:10px 18px;font-size:1rem;cursor:pointer;
            text-decoration:none;margin-top:10px}
button:hover,.btn:hover{background:var(--accent-d)}
.btn.sec,button.sec{background:#eef2f7;color:var(--accent)}
.btn.sec:hover,button.sec:hover{background:#e2e8f1}
button.warn{background:#fff;color:#b3261e;border:1px solid #edc9c5}
button.warn:hover{background:#fdf3f2}
.note{background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;
      padding:10px 14px;margin:12px 0;font-size:.92rem}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.8rem}
.pill.ok{background:#dcfce7;color:#166534}
.pill.off{background:#fee2e2;color:#991b1b}
.row{display:flex;gap:10px}.row>*{flex:1}
.topbar{display:flex;justify-content:space-between;align-items:center;
        max-width:640px;margin:0 auto;padding:14px 16px 0}
.topbar a{color:var(--muted);text-decoration:none;font-size:.9rem}
code{background:#f3f4f6;padding:1px 6px;border-radius:6px;font-size:.9em}
.center{text-align:center}
.signin-mount{min-height:220px;display:flex;align-items:center;justify-content:center}
.help{font-size:.8rem;color:var(--muted);margin-top:2px}
"""


def page(request: Request, title: str, body: str, email: str = None) -> HTMLResponse:
    top = ""
    if email:
        admin = '<a href="/admin">Admin</a> · ' if _is_admin(email) else ""
        top = ('<div class="topbar"><span class="muted">' + _esc(email)
               + '</span>' + admin + '<a href="/logout">Abmelden</a></div>')
    return HTMLResponse(
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + _esc(title) + '</title><style>' + _CSS + '</style></head>'
        '<body>' + top + '<main>' + body + '</main></body></html>'
    )


def _clerk_login_html() -> str:
    pk = _esc(clerk_auth.publishable_key())
    src = _esc(clerk_auth.clerk_js_url())
    return (
        '<div id="clerk-signin" class="signin-mount"><div class="loading">Lade Anmeldung…</div></div>'
        '<script async crossorigin="anonymous" data-clerk-publishable-key="' + pk + '" '
        'src="' + src + '" onload="'
        "var el=document.getElementById('clerk-signin');"
        "window.addEventListener('load',async function(){"
        "try{"
        "await Clerk.load();"
        "if(Clerk.user){window.location='/auth/clerk';return;}"
        "Clerk.mountSignIn(el,{afterSignInUrl:'/auth/clerk',afterSignUpUrl:'/auth/clerk',"
        "appearance:{variables:{colorPrimary:'#1d4ed8'}}});"
        "}catch(err){"
        "el.innerHTML='<p>Anmeldung fehlgeschlagen — bitte Seite neu laden.</p>';"
        "console.error(err);"
        "}"
        "});"
        '"></script>'
    )


# ── Login ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if _auth(request):
        return RedirectResponse("/dashboard", status_code=303)
    if clerk_auth.configured():
        primary = _clerk_login_html()
    else:
        primary = ('<div class="card"><p class="muted">Login nicht konfiguriert '
                   '(CLERK_PUBLISHABLE_KEY fehlt).</p></div>')
    dev = ""
    if ALLOW_DEV_LOGIN:
        dev = ('<form method="post" action="/login/dev" class="card">'
               '<label>Dev-Login (nur lokal)</label>'
               '<input name="email" type="email" required placeholder="du@example.com">'
               '<button style="margin-top:10px">Anmelden</button></form>')
    body = ('<div class="hero"><h1>München Wohnungsmonitor</h1>'
            '<p class="muted">Finde deine Mietwohnung in München — automatisch '
            'per Telegram.</p>'
            '<div class="note">🔑 Einladung erforderlich — du brauchst einen '
            'Einladungscode vom Betreiber.</div>'
            f'{primary}{dev}</div>')
    return page(request, "Anmelden", body)


@app.get("/auth/clerk")
def auth_clerk(request: Request):
    claims = clerk_auth.verify_session_token(request.cookies.get("__session", ""))
    if not claims:
        return page(request, "Anmelden",
                    '<div class="card">Anmeldung fehlgeschlagen — bitte erneut '
                    'versuchen. <a href="/">Zurück</a>.</div>')
    email = clerk_auth.resolve_email(claims)
    uid = (claims.get("sub") or "").strip()
    if not email or not uid:
        return page(request, "Anmelden",
                    '<div class="card">Keine E-Mail im Konto gefunden. '
                    '<a href="/">Zurück</a>.</div>')
    request.session["email"] = email
    request.session["uid"] = uid
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/login/dev")
def login_dev(request: Request, email: str = Form(...)):
    if not ALLOW_DEV_LOGIN:
        raise HTTPException(404)
    email = email.lower().strip()
    request.session["email"] = email
    request.session["uid"] = "dev:" + email
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ── Onboarding (Einladungscode) ────────────────────────────────────────────

@app.get("/onboard", response_class=HTMLResponse)
def onboard_form(request: Request):
    uid, email = _require(request)
    if profiles.get_user(uid):
        return RedirectResponse("/dashboard", status_code=303)
    body = ('<h1>Einladungscode</h1>'
            '<form method="post" action="/onboard" class="card">'
            '<label>Dein Einladungscode</label>'
            '<input name="invite" required placeholder="z.B. WOHN-X7K2MP" autocomplete="off">'
            '<p class="help">Hast du keinen Code? Frag den Betreiber — die '
            'Registrierung ist nur mit Einladung möglich.</p>'
            '<button>Einlösung prüfen</button></form>')
    return page(request, "Einladung", body, email)


@app.post("/onboard")
def onboard_submit(request: Request, invite: str = Form(...)):
    uid, email = _require(request)
    if profiles.get_user(uid):
        return RedirectResponse("/dashboard", status_code=303)
    if not ratelimit.hit(f"onboard:{uid}", [(5, 600)])[0]:
        return page(request, "Einladung",
                    '<div class="card"><b>Zu viele Versuche.</b> '
                    '<a href="/onboard">Erneut versuchen</a>.</div>', email)
    # Codes sind Großbuchstaben (CLI erzeugt sie so) — klein geschriebene
    # Eingaben normalisieren, damit Abtippen nicht unnötig scheitert.
    if not profiles.use_invite(invite.strip().upper(), email):
        return page(request, "Einladung",
                    '<div class="card"><b>Ungültiger oder bereits benutzter '
                    'Einladungscode.</b> <a href="/onboard">Zurück</a>.</div>', email)
    # Erst der atomare Code-Verbrauch, dann das Profil — so kann ein Code nie
    # doppelt genutzt werden.
    profiles.upsert_user(uid, email=email, name=email.partition("@")[0])
    return RedirectResponse("/dashboard", status_code=303)


# ── Dashboard ──────────────────────────────────────────────────────────────

def _filters_form(prof) -> str:
    f = prof.filters
    bez = ", ".join(f.get("bezirke_erlaubt", []) or [])
    return (
        '<form method="post" action="/settings" class="card">'
        '<p class="muted" style="margin-top:0">Welche Wohnungen sollen dir '
        'per Telegram geschickt werden?</p>'
        '<div class="row">'
        f'<div><label>Max. Miete warm (€)</label>'
        f'<input name="max_warm_miete" inputmode="decimal" value="{_esc(_fmt(f.get("max_warm_miete")))}" '
        'placeholder="2200"></div>'
        f'<div><label>Max. Miete kalt (€)</label>'
        f'<input name="max_kalt_miete" inputmode="decimal" value="{_esc(_fmt(f.get("max_kalt_miete")))}" '
        'placeholder="1950"></div></div>'
        '<div class="row">'
        f'<div><label>Min. Größe (qm)</label>'
        f'<input name="min_groesse" inputmode="decimal" value="{_esc(_fmt(f.get("min_groesse")))}" '
        'placeholder="45"></div>'
        f'<div><label>Min. Zimmer</label>'
        f'<input name="min_zimmer" inputmode="decimal" step="0.5" value="{_esc(_fmt(f.get("min_zimmer"), ".1f"))}" '
        'placeholder="1,5"></div></div>'
        '<div class="row">'
        f'<div><label>Max. Umkreis zum Zentrum (km)</label>'
        f'<input name="max_radius_km" inputmode="decimal" value="{_esc(_fmt(f.get("max_radius_km"), ".1f"))}" '
        'placeholder="4,0"></div></div>'
        f'<label>Nur diese Stadtteile (kommagetrennt, optional)</label>'
        f'<input name="bezirke_erlaubt" value="{_esc(bez)}" '
        'placeholder="schwabing, maxvorstadt, glockenbachviertel">'
        '<p class="help">Leer lassen = alle erlaubten Innenstadt-Bezirke. '
        'Außenbezirke sind grundsätzlich ausgeschlossen.</p>'
        '<button>Speichern</button></form>'
    )


def _telegram_card(prof) -> str:
    if prof.chat_ids:
        liste = ", ".join(_esc(c) for c in prof.chat_ids)
        return (f'<p>✅ Telegram verbunden ({len(prof.chat_ids)} Chat): '
                f'<code>{liste}</code></p>'
                '<p class="muted">Du bekommst ab jetzt passende Wohnungen '
                'direkt hierher geschickt.</p>')
    code = profiles.new_pairing_code(prof.uid)
    return (
        f'<p>Verbinde Telegram, um Matches zu bekommen:</p>'
        f'<a class="btn" href="https://t.me/{_esc(BOT_USERNAME)}?start={_esc(code)}">'
        'Telegram verknüpfen</a>'
        f'<p class="muted">Oder schreib dem Bot <code>/start {_esc(code)}</code>. '
        'Der Code ist 30 Minuten gültig.</p>'
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    uid, email = _require(request)
    prof = profiles.get_user(uid)
    if not prof:
        return RedirectResponse("/onboard", status_code=303)
    status = ('<span class="pill ok">aktiv</span>' if prof.active
              else '<span class="pill off">pausiert</span>')
    flash = ('<p class="pill ok">✅ Filter gespeichert</p>'
             if request.query_params.get("saved") else "")
    body = (f'<h1>Dein Dashboard</h1>'
            f'<div class="card"><b>{_esc(prof.name or email)}</b> — {status}<br>'
            f'<span class="muted">{_esc(email)}</span></div>'
            '<div class="card"><h2>📲 Telegram</h2>' + _telegram_card(prof) + '</div>'
            '<div class="card"><h2>🎯 Deine Filter</h2>' + flash + _filters_form(prof) + '</div>'
            '<div class="card"><h2>Einstellungen</h2>'
            f'<form method="post" action="/{"pause" if prof.active else "resume"}" style="display:inline">'
            f'<button class="sec">{"Pausieren" if prof.active else "Fortsetzen"}</button></form>'
            '<form method="post" action="/delete" style="display:inline;margin-left:8px" '
            'onsubmit="return confirm(\'Wirklich alle Daten löschen?\')">'
            '<button class="warn">Konto & Daten löschen</button></form></div>')
    return page(request, "Dashboard", body, email)


# ── Filter speichern / Pause / Löschen ─────────────────────────────────────

@app.post("/settings")
def settings(request: Request,
             max_warm_miete: str = Form(""),
             max_kalt_miete: str = Form(""),
             min_groesse: str = Form(""),
             min_zimmer: str = Form(""),
             max_radius_km: str = Form(""),
             bezirke_erlaubt: str = Form("")):
    uid, _ = _require(request)
    if not profiles.get_user(uid):
        return RedirectResponse("/onboard", status_code=303)
    if not ratelimit.hit(f"settings:{uid}", [(20, 300)])[0]:
        return RedirectResponse("/dashboard?saved=1", status_code=303)
    values = {"max_warm_miete": max_warm_miete, "max_kalt_miete": max_kalt_miete,
              "min_groesse": min_groesse, "min_zimmer": min_zimmer,
              "max_radius_km": max_radius_km, "bezirke_erlaubt": bezirke_erlaubt}
    profiles.update_filters(uid, _parse_filters(values))
    return RedirectResponse("/dashboard?saved=1", status_code=303)


@app.post("/pause")
def pause(request: Request):
    uid, _ = _require(request)
    if profiles.get_user(uid):
        profiles.set_active(uid, False)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/resume")
def resume(request: Request):
    uid, _ = _require(request)
    if profiles.get_user(uid):
        profiles.set_active(uid, True)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/delete")
def delete(request: Request):
    uid, _ = _require(request)
    if profiles.get_user(uid):
        profiles.delete_user(uid)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ── Admin-Panel ─────────────────────────────────────────────────────────────

def _state_stats() -> dict:
    """Kennzahlen aus den State-Dateien (seen/delivered/matches) — die Webapp
    läuft im Projekt-WorkingDirectory, die Dateien liegen direkt dort."""
    stats = {"seen": 0, "letzter_lauf": "–",
             "delivered_eintraege": 0, "delivered_nachrichten": 0,
             "matches_gesamt": 0, "matches_heute": 0, "matches_pro_quelle": {},
             "letzte_matches": []}
    try:
        s = Path("seen.json")
        stats["seen"] = len(json.loads(s.read_text()).get("ids", []))
        stats["letzter_lauf"] = datetime.fromtimestamp(s.stat().st_mtime) \
            .strftime("%d.%m.%Y %H:%M")
    except Exception:
        pass
    try:
        d = json.loads(Path("delivered.json").read_text())
        stats["delivered_eintraege"] = len(d)
        stats["delivered_nachrichten"] = sum(len(v) for v in d.values())
    except Exception:
        pass
    try:
        ms = json.loads(Path("matches.json").read_text())
        stats["matches_gesamt"] = len(ms)
        heute = date.today().strftime("%d.%m.")
        quelle: dict = {}
        for m in ms:
            q = m.get("quelle", "?")
            quelle[q] = quelle.get(q, 0) + 1
            if (m.get("gefunden_um") or "")[:5] == heute:
                stats["matches_heute"] += 1
        stats["matches_pro_quelle"] = dict(sorted(quelle.items(), key=lambda x: -x[1]))
        stats["letzte_matches"] = ms[-10:][::-1]
    except Exception:
        pass
    return stats


def _admin_html(request: Request, email: str, flash: str = "") -> HTMLResponse:
    stats = _state_stats()
    users = profiles.list_users()
    invites = profiles.list_invites()

    nutzer_aktiv = sum(1 for u in users if u["active"])
    nutzer_chat = sum(1 for u in users if u["chat_ids"])
    nutzer_filter = sum(1 for u in profiles.load_active_profiles() if u.filters)

    def _karte(label: str, wert) -> str:
        return (f'<div class="card" style="flex:1;margin:0;text-align:center">'
                f'<div style="font-size:1.7rem;font-weight:700">{_esc(wert)}</div>'
                f'<div class="muted">{_esc(label)}</div></div>')

    cards = (_karte("Nutzer", len(users)) + _karte("davon aktiv", nutzer_aktiv)
             + _karte("mit Telegram", nutzer_chat) + _karte("mit eigenen Filtern", nutzer_filter)
             + _karte("Matches gesamt", stats["matches_gesamt"])
             + _karte("heute", stats["matches_heute"])
             + _karte("bekannte Inserate", stats["seen"])
             + _karte("Zustellungen", stats["delivered_nachrichten"]))

    quellen = " · ".join(f"{_esc(q)}: {n}" for q, n in stats["matches_pro_quelle"].items()) \
        or "–"

    # Nutzer-Tabelle
    if users:
        rows = "".join(
            f'<tr><td>{_esc(u["uid"][:24])}</td><td>{_esc(u["email"] or "–")}</td>'
            f'<td>{"✅" if u["active"] else "⏸"}</td>'
            f'<td>{", ".join(_esc(c) for c in u["chat_ids"]) or "–"}</td>'
            f'<td class="muted">{_esc((u.get("created_at") or "")[:10])}</td></tr>'
            for u in users)
        nutzer_tabelle = ('<table style="width:100%;border-collapse:collapse;font-size:.9rem">'
                          '<tr><th align="left">uid</th><th align="left">E-Mail</th>'
                          '<th align="left">aktiv</th><th align="left">Telegram</th>'
                          '<th align="left">registriert</th></tr>' + rows + '</table>')
    else:
        nutzer_tabelle = '<p class="muted">Noch keine Nutzer.</p>'

    # Einladungscodes
    code_rows = "".join(
        f'<tr><td><code>{_esc(i["code"])}</code></td>'
        f'<td>{"✅ benutzt" if i["used"] else "🟢 offen"}</td>'
        f'<td class="muted">{_esc(i.get("used_by") or "")}</td>'
        f'<td>' + ('' if i["used"] else
                   f'<form method="post" action="/admin/revoke" style="margin:0">'
                   f'<input type="hidden" name="code" value="{_esc(i["code"])}">'
                   f'<button class="warn" style="padding:2px 10px;margin:0">widerrufen</button>'
                   f'</form>') + '</td></tr>'
        for i in invites)
    code_tabelle = ('<table style="width:100%;border-collapse:collapse;font-size:.9rem">'
                    '<tr><th align="left">Code</th><th align="left">Status</th>'
                    '<th align="left">benutzt von</th><th></th></tr>' + code_rows + '</table>')

    # Letzte Matches
    if stats["letzte_matches"]:
        mrows = "".join(
            f'<li>🔗 <a href="{_esc(m.get("url","#"))}" target="_blank" rel="noopener">'
            f'{_esc((m.get("titel") or "")[:70])}</a> '
            f'<span class="muted">— {_esc(str(m.get("preis_warm","")))}€ · '
            f'{_esc(str(m.get("groesse","")))}qm · {_esc(m.get("quelle",""))}'
            f' · {_esc(m.get("gefunden_um",""))}</span></li>'
            for m in stats["letzte_matches"])
        letzte = '<ul style="padding-left:18px;margin:4px 0">' + mrows + '</ul>'
    else:
        letzte = '<p class="muted">Noch keine Matches.</p>'

    body = (
        '<h1>Admin-Panel</h1>' + (f'<p class="pill ok">{_esc(flash)}</p>' if flash else "")
        + '<div class="card"><h2>Übersicht</h2>'
        f'<div style="display:flex;gap:10px;flex-wrap:wrap">{cards}</div></div>'
        '<div class="card"><h2>🎟 Einladungscodes</h2>'
        '<form method="post" action="/admin/codes" style="display:flex;gap:8px;align-items:end">'
        '<div><label>Anzahl</label>'
        '<input name="count" type="number" min="1" max="50" value="1" style="width:80px"></div>'
        '<div><label>Präfix</label>'
        f'<input name="prefix" value="WOHN" style="width:120px" maxlength="12"></div>'
        '<button>Erzeugen</button></form>'
        f'<p class="muted">Codes sind single-use und werden beim Onboarding verbraucht.</p>'
        f'{code_tabelle}</div>'
        '<div class="card"><h2>📈 Kennzahlen</h2>'
        f'<p class="muted" style="margin-top:0">Letzter Engine-Lauf: {_esc(stats["letzter_lauf"])} '
        f'· Matches nach Quelle: {quellen}</p>'
        f'{nutzer_tabelle}</div>'
        '<div class="card"><h2>🕘 Letzte Matches</h2>' + letzte + '</div>'
    )
    return page(request, "Admin", body, email)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    _, email = _require_admin(request)
    return _admin_html(request, email)


@app.post("/admin/codes")
def admin_codes(request: Request, count: int = Form(1), prefix: str = Form("WOHN")):
    uid, email = _require_admin(request)
    if not ratelimit.hit(f"admin-codes:{uid}", [(10, 600)])[0]:
        return _admin_html(request, email, "Zu viele Anfragen — kurz warten.")
    count = max(1, min(count, 50))
    prefix = re.sub(r"[^A-Za-z0-9]", "", prefix)[:12] or "WOHN"
    erzeugt = 0
    for _ in range(count):
        if profiles.add_invite(profiles.generate_invite_code(prefix)):
            erzeugt += 1
    return _admin_html(request, email, f"{erzeugt} Code(s) erzeugt.")


@app.post("/admin/revoke")
def admin_revoke(request: Request, code: str = Form("")):
    uid, email = _require_admin(request)
    if not ratelimit.hit(f"admin-revoke:{uid}", [(20, 300)])[0]:
        return _admin_html(request, email, "Zu viele Anfragen — kurz warten.")
    code = code.strip().upper()
    ok = profiles.use_invite(code, "(revoked)") if code else False
    return _admin_html(request, email, f"Code {code} widerrufen." if ok else f"Code {code} nicht gefunden.")


@app.get("/healthz")
def healthz():
    return {"ok": True, "clerk_login": clerk_auth.configured(),
            "clerk_domain": clerk_auth.frontend_api(), "dev_login": ALLOW_DEV_LOGIN}
