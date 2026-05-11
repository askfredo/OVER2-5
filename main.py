"""
OVER2.5 BOT — Railway + Discord Webhook
========================================
- Cada 5 días: escanea ligas y manda señales a Discord
- Cada día a medianoche: revisa partidos jugados y reporta si fue Over o no
- Variables de entorno necesarias:
    DISCORD_WEBHOOK_URL  → tu webhook de Discord
    FOOTBALL_API_KEY     → tu key de football-data.org (opcional, usa la del código si no está)
"""

import os
import json
import time
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from datetime import datetime, timedelta, date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# =============================================
# CONFIGURACIÓN
# =============================================

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
API_KEY             = os.environ.get("FOOTBALL_API_KEY", "da8df76a845d4ae0b4cc8938e9d4a9d6")
HEADERS             = {"X-Auth-Token": API_KEY}
BASE_URL            = "https://api.football-data.org/v4"
TEMPORADA_ACTUAL    = 2024

LIGAS = {
    "Premier League": "PL",
    "La Liga":        "PD",
    "Bundesliga":     "BL1",
    "Serie A":        "SA",
    "Ligue 1":        "FL1",
    "Eredivisie":     "DED",
    "Primeira Liga":  "PPL",
}

RACHA_MINIMA    = 0.60
GA_MINIMO       = 1.30
DIFF_POS_TRIPLE = 10
VENTANA_RACHA   = 5
RACHA_MIN_LAB   = 0.80
RACHA_AVG_LAB   = 0.75
MESES_ENE_MAY   = {1, 2, 3, 4, 5}

PROB = {
    "ELITE":   {"over25": 77, "label": "🔥 ELITE"},
    "TRIPLE+": {"over25": 69, "label": "⭐ TRIPLE+"},
    "FUERTE":  {"over25": 75, "label": "✅ FUERTE"},
    "SEÑAL":   {"over25": 62, "label": "🔵 SEÑAL"},
}

# Archivo donde guardamos las alertas pendientes de resolución
ALERTAS_FILE = "alertas_pendientes.json"

# =============================================
# UTILIDADES DISCORD
# =============================================

def send_discord(content: str, embeds: list = None):
    """Manda un mensaje al webhook de Discord."""
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL no configurada.")
        return

    payload = {}
    if content:
        payload["content"] = content[:2000]  # límite Discord
    if embeds:
        payload["embeds"] = embeds[:10]       # máximo 10 embeds por mensaje

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[ERROR Discord] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[ERROR Discord] {e}")


def send_discord_chunks(messages: list, title: str = ""):
    """Manda varios mensajes en chunks para no superar el límite."""
    if title:
        send_discord(title)
        time.sleep(0.5)
    for msg in messages:
        send_discord(msg)
        time.sleep(0.5)  # rate limit Discord

# =============================================
# PERSISTENCIA DE ALERTAS
# =============================================

def cargar_alertas() -> list:
    if os.path.exists(ALERTAS_FILE):
        with open(ALERTAS_FILE, "r") as f:
            return json.load(f)
    return []


def guardar_alertas(alertas: list):
    with open(ALERTAS_FILE, "w") as f:
        json.dump(alertas, f, ensure_ascii=False, indent=2)


def agregar_alertas_pendientes(nuevas: list):
    existentes = cargar_alertas()
    # Evitar duplicados por partido+fecha
    keys_existentes = {(a["fecha"], a["home"], a["away"]) for a in existentes}
    for a in nuevas:
        key = (a["fecha"], a["home"], a["away"])
        if key not in keys_existentes:
            a["resultado"] = None  # pendiente
            existentes.append(a)
    guardar_alertas(existentes)

# =============================================
# LÓGICA DEL SCANNER (igual que el original)
# =============================================

def calcular_estado_liga(liga_code):
    url = f"{BASE_URL}/competitions/{liga_code}/matches?season={TEMPORADA_ACTUAL}&status=FINISHED"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        print("  Rate limit — esperando 65s...")
        time.sleep(65)
        r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None

    partidos = sorted(r.json().get("matches", []), key=lambda x: x["utcDate"])
    stats = defaultdict(lambda: {
        "pts": 0, "gf": 0, "ga": 0, "jugados": 0,
        "historial_over25": [], "historial_btts": [], "nombre": ""
    })

    for match in partidos:
        home_id = match["homeTeam"]["id"]
        away_id = match["awayTeam"]["id"]
        score   = match["score"]["fullTime"]
        gh, ga  = score["home"], score["away"]
        if gh is None or ga is None:
            continue

        stats[home_id]["nombre"] = match["homeTeam"]["name"]
        stats[away_id]["nombre"] = match["awayTeam"]["name"]
        total  = gh + ga
        over25 = 1 if total > 2 else 0

        stats[home_id]["jugados"] += 1; stats[away_id]["jugados"] += 1
        stats[home_id]["gf"] += gh;     stats[home_id]["ga"] += ga
        stats[away_id]["gf"] += ga;     stats[away_id]["ga"] += gh
        stats[home_id]["historial_over25"].append(over25)
        stats[away_id]["historial_over25"].append(over25)
        stats[home_id]["historial_btts"].append(1 if gh > 0 else 0)
        stats[away_id]["historial_btts"].append(1 if ga > 0 else 0)

        if   gh > ga:  stats[home_id]["pts"] += 3
        elif gh == ga: stats[home_id]["pts"] += 1; stats[away_id]["pts"] += 1
        else:          stats[away_id]["pts"] += 3

    ranking = sorted(
        stats.items(),
        key=lambda x: (-x[1]["pts"], -(x[1]["gf"] - x[1]["ga"]), -x[1]["gf"])
    )

    equipos = []
    for pos, (eq_id, s) in enumerate(ranking, 1):
        j = s["jugados"]
        if j < 5:
            continue
        hist_o25  = s["historial_over25"][-VENTANA_RACHA:]
        hist_btts = s["historial_btts"][-VENTANA_RACHA:]
        equipos.append({
            "id":         eq_id,
            "nombre":     s["nombre"],
            "pos":        pos,
            "jugados":    j,
            "avg_ga":     round(s["ga"] / j, 2),
            "racha_o25":  round(sum(hist_o25)  / len(hist_o25)  if hist_o25  else 0, 2),
            "racha_btts": round(sum(hist_btts) / len(hist_btts) if hist_btts else 0, 2),
        })

    return equipos


def get_proximos_dias(liga_code, dias=5):
    hoy       = datetime.utcnow().date()
    en_n_dias = hoy + timedelta(days=dias)
    url       = f"{BASE_URL}/competitions/{liga_code}/matches?status=SCHEDULED"
    r         = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return []
    return [
        m for m in r.json().get("matches", [])
        if hoy <= datetime.fromisoformat(m["utcDate"].replace("Z", "")).date() <= en_n_dias
    ]


def evaluar_partido(home_id, away_id, fecha_str, liga, equipos_list):
    equipos = {e["id"]: e for e in equipos_list}
    home = equipos.get(home_id)
    away = equipos.get(away_id)
    if not home or not away:
        return None

    racha_o25_h  = home["racha_o25"]
    racha_o25_a  = away["racha_o25"]
    racha_btts_h = home["racha_btts"]
    racha_btts_a = away["racha_btts"]
    ga_h         = home["avg_ga"]
    ga_a         = away["avg_ga"]
    diff_pos     = abs(int(home["pos"]) - int(away["pos"]))

    racha_min   = min(racha_o25_h, racha_o25_a)
    racha_avg   = (racha_o25_h + racha_o25_a) / 2
    mes_partido = int(fecha_str[5:7])
    en_ene_may  = mes_partido in MESES_ENE_MAY

    racha_o25_ok  = (racha_o25_h >= RACHA_MINIMA) and (racha_o25_a >= RACHA_MINIMA)
    racha_btts_ok = (racha_btts_h >= RACHA_MINIMA) and (racha_btts_a >= RACHA_MINIMA)
    defensa_ok    = (ga_h >= GA_MINIMO) or (ga_a >= GA_MINIMO)
    diff_ok       = diff_pos >= DIFF_POS_TRIPLE
    triple_base   = racha_o25_ok and racha_btts_ok and defensa_ok and diff_ok

    if not racha_o25_ok:
        return None

    cond1 = racha_min >= RACHA_MIN_LAB
    cond2 = cond1 and en_ene_may
    cond3 = racha_avg >= RACHA_AVG_LAB and en_ene_may

    if triple_base and cond2:
        nivel = "ELITE"
    elif triple_base and cond3 and not cond1:
        nivel = "FUERTE"
    elif triple_base and cond1:
        nivel = "TRIPLE+"
    elif racha_o25_ok and defensa_ok:
        nivel = "SEÑAL"
    else:
        return None

    info_nivel = PROB[nivel]
    justo      = round(info_nivel["over25"] / 100, 2)

    return {
        "nivel":        nivel,
        "badge":        info_nivel["label"],
        "prob_over25":  info_nivel["over25"],
        "poly_justo":   justo,
        "fecha":        fecha_str,
        "liga":         liga,
        "home":         home["nombre"],
        "away":         away["nombre"],
        "home_id":      home_id,
        "away_id":      away_id,
        "pos_home":     int(home["pos"]),
        "pos_away":     int(away["pos"]),
        "diff_pos":     diff_pos,
        "racha_o25_h":  racha_o25_h,
        "racha_o25_a":  racha_o25_a,
        "racha_min":    round(racha_min, 2),
        "racha_avg":    round(racha_avg, 2),
        "racha_btts_h": racha_btts_h,
        "racha_btts_a": racha_btts_a,
        "ga_home":      ga_h,
        "ga_away":      ga_a,
        "en_ene_may":   "Sí" if en_ene_may else "No",
        "resultado":    None,
    }

# =============================================
# JOB 1: SCANNER CADA 5 DÍAS
# =============================================

def job_scanner():
    print(f"\n[{datetime.utcnow()}] 🔍 Iniciando scanner 5-días...")
    hoy            = datetime.utcnow().date()
    en_5_dias      = hoy + timedelta(days=5)
    mes_actual     = hoy.month
    es_temp_alta   = mes_actual in MESES_ENE_MAY
    alertas_nuevas = []

    for nombre_liga, codigo in LIGAS.items():
        print(f"  Escaneando {nombre_liga}...")
        equipos = calcular_estado_liga(codigo)
        time.sleep(7)
        if not equipos:
            continue

        proximos = get_proximos_dias(codigo, dias=5)
        time.sleep(7)

        for match in proximos:
            res = evaluar_partido(
                match["homeTeam"]["id"],
                match["awayTeam"]["id"],
                match["utcDate"][:10],
                nombre_liga,
                equipos
            )
            if res:
                alertas_nuevas.append(res)

    if not alertas_nuevas:
        send_discord(f"📭 **Scanner {hoy} → {en_5_dias}**\nSin alertas Over2.5 para este período.")
        return

    # Guardar en archivo para el seguimiento diario
    agregar_alertas_pendientes(alertas_nuevas)

    # Ordenar por nivel
    orden = {"ELITE": 0, "TRIPLE+": 1, "FUERTE": 2, "SEÑAL": 3}
    alertas_nuevas.sort(key=lambda x: (orden.get(x["nivel"], 9), x["fecha"]))

    # ── Armar mensajes Discord ──
    modo = "🔥 modo ENE–MAY (alta precisión)" if es_temp_alta else "⭐ modo AGO–DIC (estándar)"
    header = (
        f"# 🏟️ SCANNER OVER2.5 — {hoy} → {en_5_dias}\n"
        f"**{len(alertas_nuevas)} alertas encontradas** | {modo}\n"
        f"{'─'*40}"
    )
    send_discord(header)
    time.sleep(0.5)

    for a in alertas_nuevas:
        ganancia = round(a["poly_justo"] - 0.50, 2)
        msg = (
            f"## {a['badge']}  `{a['fecha']}`  [{a['liga']}]\n"
            f"**{a['home']}** (#{a['pos_home']}) vs **{a['away']}** (#{a['pos_away']})\n"
            f"```\n"
            f"Racha O25  : {a['racha_o25_h']:.0%} / {a['racha_o25_a']:.0%}  (mín {a['racha_min']:.0%})\n"
            f"Racha BTTS : {a['racha_btts_h']:.0%} / {a['racha_btts_a']:.0%}\n"
            f"GA         : {a['ga_home']:.2f} / {a['ga_away']:.2f}  |  diff_pos={a['diff_pos']}\n"
            f"Mes ene-may: {a['en_ene_may']}\n"
            f"```\n"
            f"📊 Prob histórica: **{a['prob_over25']}%** | Precio justo Poly: **${a['poly_justo']:.2f}**\n"
            f"💰 Compras a $0.50 → ganancia esperada **+${ganancia:.2f}** por contrato"
        )
        send_discord(msg)
        time.sleep(0.8)

    # Tabla resumen final
    tabla = "## 📋 Referencia Polymarket\n```\nNivel       Prob   Precio  Ganancia vs $0.50\n" + "─"*46 + "\n"
    for nivel in ["ELITE", "FUERTE", "TRIPLE+", "SEÑAL"]:
        info  = PROB[nivel]
        justo = round(info["over25"] / 100, 2)
        gan   = round(justo - 0.50, 2)
        tabla += f"{info['label']:<14} {info['over25']:>4}%   ${justo:.2f}   +${gan:.2f}\n"
    tabla += "```"
    send_discord(tabla)

    print(f"  ✅ {len(alertas_nuevas)} alertas enviadas a Discord.")

# =============================================
# JOB 2: SEGUIMIENTO DIARIO (resultados)
# =============================================

def get_partidos_finalizados_hoy(liga_code, fecha_str):
    """Obtiene partidos finalizados en una fecha específica."""
    url = f"{BASE_URL}/competitions/{liga_code}/matches?dateFrom={fecha_str}&dateTo={fecha_str}&status=FINISHED"
    r   = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        time.sleep(65)
        r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return []
    return r.json().get("matches", [])


def job_seguimiento_diario():
    print(f"\n[{datetime.utcnow()}] 📊 Verificando resultados de hoy...")
    ayer     = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    alertas  = cargar_alertas()
    pendientes_ayer = [a for a in alertas if a["fecha"] == ayer and a["resultado"] is None]

    if not pendientes_ayer:
        print(f"  Sin alertas pendientes para {ayer}.")
        return

    # Obtener resultados por liga
    ligas_necesarias = list({a["liga"] for a in pendientes_ayer})
    resultados_por_liga = {}

    for liga_nombre in ligas_necesarias:
        codigo = LIGAS.get(liga_nombre)
        if not codigo:
            continue
        partidos = get_partidos_finalizados_hoy(codigo, ayer)
        time.sleep(7)
        for p in partidos:
            key = (p["homeTeam"]["name"], p["awayTeam"]["name"])
            gh  = p["score"]["fullTime"]["home"]
            ga  = p["score"]["fullTime"]["away"]
            if gh is not None and ga is not None:
                resultados_por_liga[key] = {"gh": gh, "ga": ga, "total": gh + ga}

    # Actualizar alertas con resultados
    resueltas = []
    for a in alertas:
        if a["fecha"] != ayer or a["resultado"] is not None:
            continue
        key = (a["home"], a["away"])
        if key in resultados_por_liga:
            r     = resultados_por_liga[key]
            over  = r["total"] > 2
            a["resultado"]     = "OVER ✅" if over else "UNDER ❌"
            a["goles_home"]    = r["gh"]
            a["goles_away"]    = r["ga"]
            a["total_goles"]   = r["total"]
            resueltas.append(a)

    guardar_alertas(alertas)

    if not resueltas:
        print(f"  Sin resultados encontrados para {ayer}.")
        return

    # ── Armar mensaje Discord ──
    aciertos = sum(1 for a in resueltas if "✅" in a["resultado"])
    total    = len(resueltas)
    pct      = round(aciertos / total * 100) if total else 0

    header = (
        f"# 📊 RESULTADOS — {ayer}\n"
        f"**{aciertos}/{total} Over2.5** ({pct}% acierto)\n"
        f"{'─'*40}"
    )
    send_discord(header)
    time.sleep(0.5)

    for a in resueltas:
        emoji    = "✅" if "✅" in a["resultado"] else "❌"
        resultado_txt = f"{a.get('goles_home','?')} - {a.get('goles_away','?')} ({a.get('total_goles','?')} goles)"
        msg = (
            f"{emoji} **{a['badge']}** [{a['liga']}]\n"
            f"{a['home']} vs {a['away']}\n"
            f"Resultado: **{resultado_txt}** → {a['resultado']}\n"
            f"*(Prob estimada era {a['prob_over25']}%)*"
        )
        send_discord(msg)
        time.sleep(0.6)

    print(f"  ✅ {len(resueltas)} resultados enviados.")

# =============================================
# SERVIDOR HTTP (necesario para Railway)
# =============================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        alertas = cargar_alertas()
        pendientes = [a for a in alertas if a["resultado"] is None]
        body = json.dumps({
            "status": "running",
            "alertas_totales": len(alertas),
            "pendientes": len(pendientes),
            "timestamp": datetime.utcnow().isoformat()
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silenciar logs de HTTP


def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[HTTP] Servidor en puerto {port}")
    server.serve_forever()

# =============================================
# ARRANQUE
# =============================================

if __name__ == "__main__":
    print("="*55)
    print("  OVER2.5 BOT — Iniciando...")
    print(f"  Webhook: {'configurado ✅' if DISCORD_WEBHOOK_URL else 'NO configurado ❌'}")
    print("="*55)

    # Servidor HTTP en hilo separado (Railway lo necesita para saber que el proceso vive)
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    # Scheduler
    scheduler = BackgroundScheduler(timezone="UTC")

    # Job 1: Scanner → cada 5 días a las 08:00 UTC
    scheduler.add_job(
        job_scanner,
        trigger=IntervalTrigger(days=5),
        id="scanner",
        next_run_time=datetime.utcnow() + timedelta(seconds=10)  # primera vez en 10s al arrancar
    )

    # Job 2: Seguimiento → cada día a las 00:30 UTC
    scheduler.add_job(
        job_seguimiento_diario,
        trigger=CronTrigger(hour=0, minute=30),
        id="seguimiento"
    )

    scheduler.start()
    print("  Scheduler activo. Jobs programados:")
    print("  → Scanner: cada 5 días (primera vez en 10s)")
    print("  → Seguimiento: diario a las 00:30 UTC")

    # Mantener el proceso vivo
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Bot detenido.")
