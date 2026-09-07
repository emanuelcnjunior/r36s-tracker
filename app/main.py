import hashlib
import os
import posixpath
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

import paramiko
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse

DB = os.getenv("DATABASE_PATH", "/data/r36s.db")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/data/media"))
HOST = os.getenv("R36S_HOST", "192.168.0.122")
PORT = int(os.getenv("R36S_PORT", "22"))
USER = os.getenv("R36S_USER", "ark")
PASSWORD = os.getenv("R36S_PASSWORD", "")
INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
SSH_RETRIES = int(os.getenv("SSH_RETRIES", "3"))

sync_state = {
    "running": False,
    "phase": "idle",
    "system": None,
    "current": 0,
    "total": 0,
    "last_ok": None,
    "last_error": None,
}


def db_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, ddl):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with db_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            remote_playcount INTEGER NOT NULL DEFAULT 0,
            baseline_playcount INTEGER NOT NULL DEFAULT 0,
            observed_plays INTEGER NOT NULL DEFAULT 0,
            played INTEGER NOT NULL DEFAULT 0,
            lastplayed TEXT,
            genre TEXT,
            developer TEXT,
            publisher TEXT,
            image TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(system, path)
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            message TEXT
        );
        """)
        ensure_column(conn, "games", "snapshot_remote", "TEXT")
        ensure_column(conn, "games", "snapshot_local", "TEXT")
        ensure_column(conn, "games", "completed", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "games", "status", "TEXT")
        conn.execute("""
            UPDATE games
            SET status='completed'
            WHERE completed=1 AND (status IS NULL OR status='')
        """)
        conn.commit()


def text(node, tag, default=""):
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else default


def parse_int(value):
    try:
        return int(value)
    except Exception:
        return 0


def connect_ssh():
    last = None
    for attempt in range(1, SSH_RETRIES + 1):
        try:
            sync_state["phase"] = f"conectando ({attempt}/{SSH_RETRIES})"
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                HOST,
                port=PORT,
                username=USER,
                password=PASSWORD,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10,
            )
            transport = ssh.get_transport()
            if transport:
                transport.set_keepalive(15)
            return ssh
        except Exception as exc:
            last = exc
            if attempt < SSH_RETRIES:
                time.sleep(attempt * 3)
    raise last


def remote_gamelists(sftp):
    out = []
    for attr in sftp.listdir_attr("/roms"):
        system = attr.filename
        path = f"/roms/{system}/gamelist.xml"
        try:
            sftp.stat(path)
            out.append((system, path))
        except OSError:
            pass
    return sorted(out)


def remote_media_path(system, value):
    if not value:
        return None
    if value.startswith("/"):
        path = posixpath.normpath(value)
    else:
        path = posixpath.normpath(posixpath.join("/roms", system, value))
    # Segurança: só aceitamos mídia dentro de /roms.
    if not path.startswith("/roms/"):
        return None
    return path


def cache_snapshot(sftp, game_id, system, remote_value):
    remote = remote_media_path(system, remote_value)
    if not remote:
        return None

    ext = Path(remote).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        ext = ".img"

    system_dir = MEDIA_DIR / system
    system_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:16]
    local = system_dir / f"{game_id}-{digest}{ext}"

    if local.exists() and local.stat().st_size > 0:
        return str(local)

    tmp = local.with_suffix(local.suffix + ".part")
    try:
        sftp.get(remote, str(tmp))
        tmp.replace(local)
        return str(local)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def sync_once():
    if sync_state["running"]:
        return False

    sync_state.update({
        "running": True,
        "phase": "iniciando",
        "system": None,
        "current": 0,
        "total": 0,
        "last_error": None,
    })

    started = datetime.now().isoformat(timespec="seconds")
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sync_log(started_at, status) VALUES (?, 'running')",
            (started,),
        )
        log_id = cur.lastrowid
        conn.commit()

    ssh = None
    sftp = None

    try:
        ssh = connect_ssh()
        sync_state["phase"] = "abrindo SFTP"
        sftp = ssh.open_sftp()
        if sftp.get_channel():
            sftp.get_channel().settimeout(25)

        gamelists = remote_gamelists(sftp)
        sync_state["total"] = len(gamelists)

        total_games = 0

        with db_conn() as conn:
            for index, (system, path) in enumerate(gamelists, start=1):
                sync_state.update({
                    "phase": "lendo gamelist",
                    "system": system,
                    "current": index,
                })

                try:
                    with sftp.open(path, "r") as f:
                        raw = f.read()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    root = ET.fromstring(raw)
                except Exception:
                    continue

                for game in root.findall("game"):
                    rom_path = text(game, "path")
                    if not rom_path:
                        continue

                    name = text(game, "name") or rom_path
                    favorite = 1 if text(game, "favorite").lower() == "true" else 0
                    playcount = parse_int(text(game, "playcount", "0"))
                    lastplayed = text(game, "lastplayed") or None

                    # Em muitos scrapes do EmulationStation, <image> é justamente
                    # a arte/screenshot exibida no menu. Se não existir, tentamos thumbnail.
                    snapshot_remote = text(game, "image") or text(game, "thumbnail") or None

                    existing = conn.execute(
                        "SELECT * FROM games WHERE system=? AND path=?",
                        (system, rom_path),
                    ).fetchone()

                    now = datetime.now().isoformat(timespec="seconds")

                    if existing is None:
                        baseline = playcount
                        observed = 0
                        played = 0
                        conn.execute("""
                            INSERT INTO games (
                                system, path, name, favorite,
                                remote_playcount, baseline_playcount,
                                observed_plays, played, lastplayed,
                                genre, developer, publisher, image,
                                snapshot_remote, first_seen_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            system, rom_path, name, favorite,
                            playcount, baseline, observed, played, lastplayed,
                            text(game, "genre"), text(game, "developer"),
                            text(game, "publisher"), text(game, "image"),
                            snapshot_remote, now, now
                        ))
                    else:
                        baseline = existing["baseline_playcount"]
                        observed = max(0, playcount - baseline)
                        played = existing["played"]

                        conn.execute("""
                            UPDATE games SET
                                name=?, favorite=?, remote_playcount=?,
                                observed_plays=?, played=?, lastplayed=?,
                                genre=?, developer=?, publisher=?, image=?,
                                snapshot_remote=?, updated_at=?
                            WHERE system=? AND path=?
                        """, (
                            name, favorite, playcount, observed, played,
                            lastplayed, text(game, "genre"),
                            text(game, "developer"), text(game, "publisher"),
                            text(game, "image"), snapshot_remote, now,
                            system, rom_path
                        ))

                    total_games += 1

                conn.commit()

            # Cacheia mídia apenas do que faz parte do seu histórico.
            # Assim não precisamos puxar imagens dos 2001 jogos pela Wi‑Fi de uma vez.
            tracked = conn.execute("""
                SELECT id, system, snapshot_remote, snapshot_local
                FROM games
                WHERE snapshot_remote IS NOT NULL
            """).fetchall()

            sync_state.update({
                "phase": "baixando snapshots do catálogo",
                "system": None,
                "current": 0,
                "total": len(tracked),
            })

            cached = 0
            for index, g in enumerate(tracked, start=1):
                sync_state["current"] = index
                if g["snapshot_local"] and Path(g["snapshot_local"]).exists():
                    cached += 1
                    continue
                local = cache_snapshot(
                    sftp, g["id"], g["system"], g["snapshot_remote"]
                )
                if local:
                    conn.execute(
                        "UPDATE games SET snapshot_local=? WHERE id=?",
                        (local, g["id"]),
                    )
                    conn.commit()
                    cached += 1

            message = f"{total_games} jogos; {cached} snapshots em cache"
            finished = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE sync_log SET finished_at=?, status='ok', message=? WHERE id=?",
                (finished, message, log_id),
            )
            conn.commit()

        sync_state["last_ok"] = finished
        sync_state["phase"] = "idle"
        return True

    except Exception as exc:
        sync_state["last_error"] = f"{type(exc).__name__}: {exc}"
        sync_state["phase"] = "erro"
        with db_conn() as conn:
            conn.execute(
                "UPDATE sync_log SET finished_at=?, status='error', message=? WHERE id=?",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    sync_state["last_error"],
                    log_id,
                ),
            )
            conn.commit()
        return False

    finally:
        try:
            if sftp:
                sftp.close()
        except Exception:
            pass
        try:
            if ssh:
                ssh.close()
        except Exception:
            pass
        sync_state["running"] = False
        sync_state["system"] = None


def sync_loop():
    time.sleep(2)
    while True:
        sync_once()
        time.sleep(INTERVAL)


def format_lastplayed(value):
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    threading.Thread(target=sync_loop, daemon=True).start()
    yield


app = FastAPI(title="R36S Tracker v2", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "ok": True,
        "r36s_host": HOST,
        "sync_running": sync_state["running"],
        "phase": sync_state["phase"],
        "system": sync_state["system"],
        "progress": {
            "current": sync_state["current"],
            "total": sync_state["total"],
        },
        "last_ok": sync_state["last_ok"],
        "last_error": sync_state["last_error"],
    }


@app.post("/sync")
def manual_sync():
    if not sync_state["running"]:
        threading.Thread(target=sync_once, daemon=True).start()
    return RedirectResponse("/", status_code=303)





@app.post("/game/{game_id}/status")
def set_status(game_id: int, status: str = ""):
    allowed = {"", "playing", "completed"}
    if status not in allowed:
        status = ""

    with db_conn() as conn:
        conn.execute(
            """
            UPDATE games
            SET status=?,
                completed=?,
                updated_at=?
            WHERE id=?
            """,
            (
                status or None,
                1 if status == "completed" else 0,
                datetime.now().isoformat(timespec="seconds"),
                game_id,
            ),
        )
        conn.commit()

        row = conn.execute(
            "SELECT id, status, completed FROM games WHERE id=?",
            (game_id,),
        ).fetchone()

        counts = conn.execute("""
            SELECT
                SUM(CASE WHEN status='playing' THEN 1 ELSE 0 END) AS playing,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status IN ('playing','completed') THEN 1 ELSE 0 END) AS tracked
            FROM games
        """).fetchone()

    return {
        "ok": True,
        "game_id": game_id,
        "status": row["status"] if row else None,
        "counts": {
            "playing": counts["playing"] or 0,
            "completed": counts["completed"] or 0,
            "tracked": counts["tracked"] or 0,
        },
    }


@app.get("/snapshot/{game_id}")
def snapshot(game_id: int):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT snapshot_local FROM games WHERE id=?", (game_id,)
        ).fetchone()
    if not row or not row["snapshot_local"]:
        return FileResponse("/dev/null", status_code=404)
    path = Path(row["snapshot_local"])
    if not path.exists():
        return FileResponse("/dev/null", status_code=404)
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
def home(system: str | None = None, view: str = "playing", q: str = "", page: int = 1, per_page: int = 24):
    with db_conn() as conn:
        systems = conn.execute("""
            SELECT
                system,
                COUNT(*) total,
                SUM(CASE WHEN status='playing' THEN 1 ELSE 0 END) playing,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed
            FROM games
            GROUP BY system
            ORDER BY system
        """).fetchall()

        where = []
        args = []

        if system:
            where.append("system=?")
            args.append(system)

        if view == "playing":
            where.append("status='playing'")
        elif view == "completed":
            where.append("status='completed'")
        elif view == "tracked":
            where.append("status IN ('playing','completed')")

        if q.strip():
            where.append("name LIKE ?")
            args.append(f"%{q.strip()}%")

        page = max(1, page)
        per_page = min(max(6, per_page), 96)

        count_sql = "SELECT COUNT(*) AS n FROM games"
        if where:
            count_sql += " WHERE " + " AND ".join(where)
        filtered_total = conn.execute(count_sql, args).fetchone()["n"]
        total_pages = max(1, (filtered_total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages

        sql = "SELECT * FROM games"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY system, name COLLATE NOCASE LIMIT ? OFFSET ?"
        games = conn.execute(
            sql, args + [per_page, (page - 1) * per_page]
        ).fetchall()

        stats = conn.execute("""
            SELECT
                COUNT(*) total,
                SUM(CASE WHEN status='playing' THEN 1 ELSE 0 END) playing,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN status IN ('playing','completed') THEN 1 ELSE 0 END) tracked,
                SUM(CASE WHEN snapshot_local IS NOT NULL THEN 1 ELSE 0 END) snapshots
            FROM games
        """).fetchone()

        last_log = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

    cards = []
    current = None

    for g in games:
        if current != g["system"]:
            if current is not None:
                cards.append("</div></section>")
            current = g["system"]
            cards.append(f"<section><h2>{escape(current.upper())}</h2><div class='games'>")

        img = (
            f"<img loading='lazy' src='/snapshot/{g['id']}' alt='{escape(g['name'])}'>"
            if g["snapshot_local"] and Path(g["snapshot_local"]).exists()
            else "<div class='noimg'>Sem snapshot</div>"
        )
        status_label = ""
        if g["status"] == "playing":
            status_label = "🎮 Jogando"
        elif g["status"] == "completed":
            status_label = "🏁 Zerado"

        cards.append(f"""
        <article class="game-card status-{escape(g["status"] or "none")}">
          <div class="shot">{img}</div>
          <div class="body">
            <div class="title">{escape(g["name"])}</div>
            <div class="meta" data-base-genre="{escape(g["genre"] or "Gênero não informado")}">
              {escape(g["genre"] or "Gênero não informado")}
              {(" · <b>" + status_label + "</b>") if status_label else ""}
            </div>
            <div class="meta">
              playcount no R36S: {g["remote_playcount"]}
              · última vez: {escape(format_lastplayed(g["lastplayed"]))}
            </div>

            <div class="status-actions" data-game-id="{g['id']}">
              <button
                class="complete-btn status-btn {"active" if g["status"] == "playing" else ""}"
                type="button"
                data-game-id="{g['id']}"
                data-status="playing"
              >
                {"✓ Jogando" if g["status"] == "playing" else "Marcar jogando"}
              </button>

              <button
                class="complete-btn status-btn {"active" if g["status"] == "completed" else ""}"
                type="button"
                data-game-id="{g['id']}"
                data-status="completed"
              >
                {"✓ Zerado" if g["status"] == "completed" else "Marcar zerado"}
              </button>

              <button
                class="complete-btn subtle status-btn"
                type="button"
                data-game-id="{g['id']}"
                data-status=""
              >
                Limpar
              </button>
            </div>
          </div>
        </article>
        """)

    if current is not None:
        cards.append("</div></section>")

    system_links = " ".join(
        f"<a href='/?system={quote(s['system'])}&view={quote(view)}&q={quote(q)}'>"
        f"{escape(s['system'])} ({(s['playing'] or 0) + (s['completed'] or 0)}/{s['total']})</a>"
        for s in systems
    )

    def page_url(n):
        params = [f"view={quote(view)}", f"page={n}", f"per_page={per_page}"]
        if system:
            params.append(f"system={quote(system)}")
        if q:
            params.append(f"q={quote(q)}")
        return "/?" + "&".join(params)

    pagination = ""
    if total_pages > 1:
        parts = []
        if page > 1:
            parts.append(f"<a class='pagebtn' href='{page_url(page-1)}'>← Anterior</a>")

        start = max(1, page - 2)
        end = min(total_pages, page + 2)

        if start > 1:
            parts.append(f"<a class='pagebtn' href='{page_url(1)}'>1</a>")
            if start > 2:
                parts.append("<span class='dots'>…</span>")

        for n in range(start, end + 1):
            cls = "pagebtn current" if n == page else "pagebtn"
            parts.append(f"<a class='{cls}' href='{page_url(n)}'>{n}</a>")

        if end < total_pages:
            if end < total_pages - 1:
                parts.append("<span class='dots'>…</span>")
            parts.append(f"<a class='pagebtn' href='{page_url(total_pages)}'>{total_pages}</a>")

        if page < total_pages:
            parts.append(f"<a class='pagebtn' href='{page_url(page+1)}'>Próxima →</a>")

        pagination = (
            f"<div class='pagination'>"
            f"<span>Página {page} de {total_pages} · {filtered_total} jogos</span>"
            + "".join(parts)
            + "</div>"
        )

    status = "Nunca sincronizado"
    if sync_state["running"]:
        total = sync_state["total"]
        cur = sync_state["current"]
        progress = f" {cur}/{total}" if total else ""
        status = f"🔄 {escape(sync_state['phase'])}{progress}"
        if sync_state["system"]:
            status += f" · {escape(sync_state['system'])}"
    elif sync_state["last_error"]:
        status = f"🔴 offline/erro · {escape(sync_state['last_error'])}"
    elif last_log:
        status = f"🟢 ok · {escape(last_log['message'] or '')}"

    return f"""
    <!doctype html>
    <html lang="pt-BR">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <meta http-equiv="refresh" content="20">
      <title>R36S Tracker</title>
      <style>
        * {{ box-sizing:border-box; }}
        body {{
          font-family:system-ui,sans-serif;
          max-width:1200px;
          margin:28px auto;
          padding:0 18px;
          background:#111;
          color:#eee;
        }}
        a {{ color:#9ecbff; margin-right:10px; text-decoration:none; }}
        .top {{ display:flex; justify-content:space-between; gap:20px; flex-wrap:wrap; }}
        .stats {{ display:flex; gap:12px; flex-wrap:wrap; margin:20px 0; }}
        .stat {{ background:#1c1c1c; padding:14px 18px; border-radius:12px; }}
        .filters,.systems {{ margin:14px 0; line-height:2; }}
        .legend {{
          display:flex;
          gap:10px;
          flex-wrap:wrap;
          margin:12px 0 4px;
        }}
        .legend-item {{
          display:inline-block;
          padding:5px 9px;
          border-radius:999px;
          border:1px solid #333;
          font-size:.85rem;
        }}
        .legend-playing {{
          background:#18253a;
          border-color:#345b91;
        }}
        .legend-completed {{
          background:#1f3426;
          border-color:#3e7a4e;
        }}
        .search {{ margin:16px 0; display:flex; gap:8px; }}
        .search input {{
          min-width:280px; padding:10px 12px; border-radius:8px;
          border:1px solid #444; background:#191919; color:#fff;
        }}
        button {{ padding:9px 14px; cursor:pointer; }}
        section {{ margin-top:30px; }}
        .games {{
          display:grid;
          grid-template-columns:repeat(auto-fit,minmax(290px,1fr));
          gap:14px;
        }}
        article {{
          background:#1c1c1c;
          border-radius:12px;
          overflow:hidden;
          border:1px solid #282828;
          transition: background .18s ease, border-color .18s ease, box-shadow .18s ease;
        }}
        article.status-playing {{
          background:#18253a;
          border-color:#345b91;
          box-shadow:0 0 0 1px rgba(52,91,145,.18);
        }}
        article.status-completed {{
          background:#1f3426;
          border-color:#3e7a4e;
          box-shadow:0 0 0 1px rgba(62,122,78,.18);
        }}
        article.status-playing .body {{
          background:rgba(24,37,58,.82);
        }}
        article.status-completed .body {{
          background:rgba(31,52,38,.82);
        }}
        .shot {{
          width:100%;
          aspect-ratio:4/3;
          background:#090909;
          display:flex;
          align-items:center;
          justify-content:center;
        }}
        .shot img {{
          width:100%;
          height:100%;
          object-fit:contain;
          image-rendering:auto;
        }}
        .noimg {{ opacity:.45; }}
        .body {{
          padding:13px;
          transition: background .18s ease;
        }}
        .title {{ font-weight:700; font-size:1rem; }}
        .meta {{ opacity:.75; font-size:.86rem; margin-top:6px; }}
        .pill {{
          display:inline-block; margin-left:6px; padding:2px 7px;
          border-radius:999px; background:#333;
        }}
        .complete-form {{ margin-top:10px; }}
        .status-actions {{
          display:flex; gap:7px; flex-wrap:wrap; margin-top:11px;
        }}
        .complete-btn {{
          border:1px solid #444; border-radius:8px;
          background:#262626; color:#eee; padding:7px 10px;
        }}
        .complete-btn.subtle {{ opacity:.7; }}
        .complete-btn.active {{
          border-color:#8a8a8a;
          background:#3a3a3a;
          font-weight:700;
        }}
        .complete-btn:disabled {{
          opacity:.55;
          cursor:wait;
        }}
        .pagination {{
          display:flex; flex-wrap:wrap; gap:8px; align-items:center;
          margin:24px 0;
        }}
        .pagebtn {{
          display:inline-block; padding:7px 10px; border-radius:8px;
          background:#1c1c1c; border:1px solid #333; color:#9ecbff;
          margin-right:0;
        }}
        .pagebtn.current {{
          font-weight:700; color:#fff; border-color:#777;
        }}
        .dots {{ opacity:.6; padding:0 2px; }}
        small {{ opacity:.7; }}

        @media (max-width: 700px) {{
          body {{
            margin: 14px auto;
            padding: 0 10px;
          }}

          .top {{
            gap: 12px;
            align-items: stretch;
          }}

          .top > div:first-child {{
            width: 100%;
          }}

          .top form {{
            width: 100%;
          }}

          .top form button {{
            width: 100%;
            min-height: 44px;
            border-radius: 10px;
          }}

          h1 {{
            font-size: 1.55rem;
            margin-bottom: 8px;
          }}

          h2 {{
            font-size: 1.2rem;
          }}

          small {{
            display: block;
            line-height: 1.45;
          }}

          .stats {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
            margin: 14px 0;
          }}

          .stat {{
            padding: 11px 12px;
            min-width: 0;
          }}

          .stat b {{
            font-size: 1.15rem;
          }}

          .search {{
            width: 100%;
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
          }}

          .search input {{
            min-width: 0;
            width: 100%;
            min-height: 44px;
            font-size: 16px;
          }}

          .search button {{
            width: 100%;
            min-height: 44px;
            border-radius: 10px;
          }}

          .legend {{
            gap: 6px;
          }}

          .legend-item {{
            font-size: .8rem;
            padding: 5px 8px;
          }}

          .filters,
          .systems {{
            line-height: 1.55;
          }}

          .filters b,
          .systems b {{
            display: block;
            margin-bottom: 6px;
          }}

          .filters a,
          .systems a {{
            display: inline-block;
            margin: 0 6px 6px 0;
            padding: 6px 8px;
            border-radius: 8px;
            background: #191919;
            border: 1px solid #2f2f2f;
          }}

          .games {{
            grid-template-columns: 1fr;
            gap: 12px;
          }}

          article {{
            border-radius: 12px;
          }}

          .shot {{
            aspect-ratio: 16 / 10;
          }}

          .shot img {{
            object-fit: contain;
          }}

          .body {{
            padding: 12px;
          }}

          .title {{
            font-size: 1rem;
            line-height: 1.3;
          }}

          .meta {{
            font-size: .84rem;
            line-height: 1.4;
          }}

          .status-actions {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
          }}

          .status-actions .complete-btn {{
            width: 100%;
            min-height: 44px;
            padding: 9px 10px;
            font-size: .92rem;
          }}

          .status-actions .subtle {{
            grid-column: 1 / -1;
          }}

          .pagination {{
            gap: 6px;
            margin: 18px 0;
          }}

          .pagination > span:first-child {{
            width: 100%;
            margin-bottom: 4px;
          }}

          .pagebtn {{
            min-width: 40px;
            min-height: 40px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 7px 9px;
          }}

          .dots {{
            padding: 8px 2px;
          }}

          section {{
            margin-top: 22px;
          }}
        }}

        @media (max-width: 390px) {{
          .stats {{
            grid-template-columns: 1fr 1fr;
          }}

          .stat {{
            padding: 10px;
          }}

          .status-actions {{
            grid-template-columns: 1fr;
          }}

          .status-actions .subtle {{
            grid-column: auto;
          }}

          .filters a,
          .systems a {{
            font-size: .9rem;
          }}
        }}


        @supports (padding: max(0px)) {{
          body {{
            padding-left: max(18px, env(safe-area-inset-left));
            padding-right: max(18px, env(safe-area-inset-right));
            padding-bottom: max(18px, env(safe-area-inset-bottom));
          }}

          @media (max-width: 700px) {{
            body {{
              padding-left: max(10px, env(safe-area-inset-left));
              padding-right: max(10px, env(safe-area-inset-right));
              padding-bottom: max(10px, env(safe-area-inset-bottom));
            }}
          }}
        }}

      </style>
    </head>
    <body>
      <div class="top">
        <div>
          <h1>🎮 R36S Tracker</h1>
          <small>R36S: {escape(HOST)} · {status}</small>
        </div>
        <form action="/sync" method="post">
          <button type="submit">Sincronizar agora</button>
        </form>
      </div>

      <div class="stats">
        <div class="stat"><b id="count-playing">{stats["playing"] or 0}</b><br>jogando</div>
        <div class="stat"><b id="count-completed">{stats["completed"] or 0}</b><br>zerados</div>
        <div class="stat"><b id="count-tracked">{stats["tracked"] or 0}</b><br>marcados por você</div>
        <div class="stat"><b>{stats["snapshots"] or 0}</b><br>snapshots em cache</div>
        <div class="stat"><b>{stats["total"] or 0}</b><br>jogos no catálogo</div>
      </div>

      <form class="search" method="get">
        <input type="hidden" name="view" value="{escape(view)}">
        <input type="hidden" name="system" value="{escape(system or '')}">
        <input name="q" value="{escape(q)}" placeholder="Buscar jogo...">
        <button>Buscar</button>
      </form>

      <div class="legend">
        <span class="legend-item legend-playing">🎮 Jogando</span>
        <span class="legend-item legend-completed">🏁 Zerado</span>
      </div>

      <div class="filters">
        <b>Ver:</b>
        <a href="/?view=playing">🎮 Jogando</a>
        <a href="/?view=completed">🏁 Zerados</a>
        <a href="/?view=tracked">Minha lista</a>
        <a href="/?view=all">Todos os jogos</a>
      </div>

      <div class="systems">
        <b>Sistemas:</b>
        <a href="/?view={escape(view)}&q={quote(q)}">Todos</a>
        {system_links}
      </div>

      {pagination}
      {''.join(cards) if cards else '<p>Nenhum jogo nessa visualização.</p>'}
      {pagination}

      <script>
        function statusLabel(status) {{
          if (status === "playing") return "🎮 Jogando";
          if (status === "completed") return "🏁 Zerado";
          return "";
        }}

        async function updateStatus(button) {{
          const gameId = button.dataset.gameId;
          const status = button.dataset.status;
          const group = button.closest(".status-actions");
          const buttons = group.querySelectorAll(".status-btn");
          const card = group.closest("article");
          const metaLines = card.querySelectorAll(".meta");
          const genreLine = metaLines[0];

          buttons.forEach(b => b.disabled = true);

          try {{
            const response = await fetch(
              `/game/${{gameId}}/status?status=${{encodeURIComponent(status)}}`,
              {{ method: "POST" }}
            );

            if (!response.ok) {{
              throw new Error(`HTTP ${{response.status}}`);
            }}

            const data = await response.json();
            const current = data.status || "";

            card.classList.remove("status-none", "status-playing", "status-completed");
            card.classList.add(`status-${{current || "none"}}`);

            buttons.forEach(b => {{
              const st = b.dataset.status;
              b.classList.toggle("active", Boolean(st) && st === current);

              if (st === "playing") {{
                b.textContent = current === "playing" ? "✓ Jogando" : "Marcar jogando";
              }} else if (st === "completed") {{
                b.textContent = current === "completed" ? "✓ Zerado" : "Marcar zerado";
              }}
            }});

            const baseGenre =
              genreLine.dataset.baseGenre ||
              genreLine.textContent.split(" · ")[0].trim();

            genreLine.dataset.baseGenre = baseGenre;

            const label = statusLabel(current);
            genreLine.innerHTML = label
              ? `${{baseGenre}} · <b>${{label}}</b>`
              : baseGenre;

            const playing = document.getElementById("count-playing");
            const completed = document.getElementById("count-completed");
            const tracked = document.getElementById("count-tracked");

            if (playing) playing.textContent = data.counts.playing;
            if (completed) completed.textContent = data.counts.completed;
            if (tracked) tracked.textContent = data.counts.tracked;

          }} catch (err) {{
            console.error(err);
            alert("Não foi possível atualizar o status do jogo.");
          }} finally {{
            buttons.forEach(b => b.disabled = false);
          }}
        }}

        document.addEventListener("click", (event) => {{
          const button = event.target.closest(".status-btn");
          if (!button) return;
          updateStatus(button);
        }});
      </script>

    </body>
    </html>
    """
