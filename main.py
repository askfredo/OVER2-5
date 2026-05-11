"""
OVER2.5 BOT — Railway + Discord Webhook
========================================
Variables de entorno:
    DISCORD_WEBHOOK_URL  → tu webhook de Discord
    FOOTBALL_API_KEY     → tu key de football-data.org
    DATA_DIR             → opcional, por defecto /data (Railway Volume)
"""

import os, json, time, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ── Configuración ────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
API_KEY             = os.environ.get("FOOTBALL_API_KEY", "da8df76a845d4ae0b4cc8938e9d4a9d6")
HEADERS             = {"X-Auth-Token": API_KEY}
BASE_URL            = "https://api.football-data.org/v4"
TEMPORADA_ACTUAL    = 2024

# Volumen Railway: /data   — sin volumen: directorio actual
_DATA_ENV  = os.environ.get("DATA_DIR", "/data")
DATA_DIR   = _DATA_ENV if os.path.isdir(_DATA_ENV) else "."
ALERTAS_FILE = os.path.join(DATA_DIR, "alertas_pendientes.json")
STATS_FILE   = os.path.join(DATA_DIR, "stats.json")

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
NIVELES_ORDEN = ["ELITE", "FUERTE", "TRIPLE+", "SEÑAL"]

# ── Discord ──────────────────────────────────────────────────────────────────
def send_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print(f"[DISCORD-MOCK] {content[:120]}")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": content[:2000]}, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[ERROR Discord] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[ERROR Discord] {e}")

# ── Stats acumulados ─────────────────────────────────────────────────────────
def _stats_vacios():
    base = {"gana": 0, "pierde": 0}
    return {"total": dict(base), "ELITE": dict(base), "TRIPLE+": dict(base),
            "FUERTE": dict(base), "SEÑAL": dict(base), "ultima_actualizacion": None}

def cargar_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return _stats_vacios()

def guardar_stats(stats):
    stats["ultima_actualizacion"] = datetime.utcnow().isoformat()
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

def registrar_resultado_en_stats(nivel: str, es_over: bool):
    stats = cargar_stats()
    campo = "gana" if es_over else "pierde"
    stats["total"][campo] += 1
    if nivel in stats:
        stats[nivel][campo] += 1
    guardar_stats(stats)

def winrate(gana, pierde):
    total = gana + pierde
    return f"{round(gana / total * 100, 1)}%" if total else "—"

def build_scoreboard(stats, contexto=""):
    t      = stats["total"]
    total  = t["gana"] + t["pierde"]
    wr     = winrate(t["gana"], t["pierde"])
    llenos = round(t["gana"] / total * 20) if total else 0
    barra  = "█" * llenos + "░" * (20 - llenos)

    lineas = ["## 📈 SCOREBOARD ACUMULADO"]
    if contexto:
        lineas.append(contexto)
    lineas += [
        "```",
        "─" * 48,
        f"  TOTAL     {t['gana']:>4}✅   {t['pierde']:>4}❌   WR: {wr:>7}   ({total} señales)",
        f"  [{barra}]",
        "─" * 48,
    ]
    for nivel in NIVELES_ORDEN:
        n  = stats.get(nivel, {"gana": 0, "pierde": 0})
        j  = n["gana"] + n["pierde"]
        if j == 0:
            continue
        wr_n  = winrate(n["gana"], n["pierde"])
        badge = PROB[nivel]["label"]
        lineas.append(f"  {badge:<14}  {n['gana']:>4}✅   {n['pierde']:>4}❌   WR: {wr_n:>7}   ({j})")
    lineas.append("```")
    return "\n".join(lineas)

# ── Persistencia alertas ─────────────────────────────────────────────────────
def cargar_alertas():
    if os.path.exists(ALERTAS_FILE):
        with open(ALERTAS_FILE) as f:
            return json.load(f)
    return []

def guardar_alertas(alertas):
    with open(ALERTAS_FILE, "w") as f:
        json.dump(alertas, f, ensure_ascii=False, indent=2)

def agregar_alertas_pendientes(nuevas):
    existentes = cargar_alertas()
    keys = {(a["fecha"], a["home"], a["away"]) for a in existentes}
    for a in nuevas:
        if (a["fecha"], a["home"], a["away"]) not in keys:
            a["resultado"] = None
            existentes.append(a)
    guardar_alertas(existentes)

# ── Lógica fútbol ────────────────────────────────────────────────────────────
def calcular_estado_liga(liga_code):
    url = f"{BASE_URL}/competitions/{liga_code}/matches?season={TEMPORADA_ACTUAL}&status=FINISHED"
    r   = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        print("  Rate limit — esperando 65s...")
        time.sleep(65)
        r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None

    partidos = sorted(r.json().get("matches", []), key=lambda x: x["utcDate"])
    stats    = defaultdict(lambda: {"pts":0,"gf":0,"ga":0,"jugados":0,
                                    "historial_over25":[],"historial_btts":[],"nombre":""})
    for match in partidos:
        hid = match["homeTeam"]["id"]; aid = match["awayTeam"]["id"]
        sc  = match["score"]["fullTime"]; gh, ga = sc["home"], sc["away"]
        if gh is None or ga is None: continue
        stats[hid]["nombre"] = match["homeTeam"]["name"]
        stats[aid]["nombre"] = match["awayTeam"]["name"]
        o25 = 1 if gh+ga > 2 else 0
        stats[hid]["jugados"] += 1; stats[aid]["jugados"] += 1
        stats[hid]["gf"] += gh;     stats[hid]["ga"] += ga
        stats[aid]["gf"] += ga;     stats[aid]["ga"] += gh
        stats[hid]["historial_over25"].append(o25); stats[aid]["historial_over25"].append(o25)
        stats[hid]["historial_btts"].append(1 if gh>0 else 0)
        stats[aid]["historial_btts"].append(1 if ga>0 else 0)
        if gh>ga: stats[hid]["pts"]+=3
        elif gh==ga: stats[hid]["pts"]+=1; stats[aid]["pts"]+=1
        else: stats[aid]["pts"]+=3

    ranking = sorted(stats.items(), key=lambda x: (-x[1]["pts"], -(x[1]["gf"]-x[1]["ga"]), -x[1]["gf"]))
    equipos = []
    for pos, (eq_id, s) in enumerate(ranking, 1):
        j = s["jugados"]
        if j < 5: continue
        ho = s["historial_over25"][-VENTANA_RACHA:]
        hb = s["historial_btts"][-VENTANA_RACHA:]
        equipos.append({
            "id": eq_id, "nombre": s["nombre"], "pos": pos, "jugados": j,
            "avg_ga":    round(s["ga"]/j, 2),
            "racha_o25": round(sum(ho)/len(ho) if ho else 0, 2),
            "racha_btts":round(sum(hb)/len(hb) if hb else 0, 2),
        })
    return equipos

def get_proximos_dias(liga_code, dias=5):
    hoy = datetime.utcnow().date(); fin = hoy + timedelta(days=dias)
    r   = requests.get(f"{BASE_URL}/competitions/{liga_code}/matches?status=SCHEDULED",
                       headers=HEADERS, timeout=30)
    if r.status_code != 200: return []
    return [m for m in r.json().get("matches",[])
            if hoy <= datetime.fromisoformat(m["utcDate"].replace("Z","")).date() <= fin]

def evaluar_partido(home_id, away_id, fecha_str, liga, equipos_list):
    eq   = {e["id"]: e for e in equipos_list}
    home = eq.get(home_id); away = eq.get(away_id)
    if not home or not away: return None

    roh=home["racha_o25"]; roa=away["racha_o25"]
    rbh=home["racha_btts"]; rba=away["racha_btts"]
    gah=home["avg_ga"]; gaa=away["avg_ga"]
    dp = abs(int(home["pos"])-int(away["pos"]))
    rmin=min(roh,roa); ravg=(roh+roa)/2
    mes=int(fecha_str[5:7]); em=mes in MESES_ENE_MAY

    o25ok  = roh>=RACHA_MINIMA and roa>=RACHA_MINIMA
    bttsok = rbh>=RACHA_MINIMA and rba>=RACHA_MINIMA
    defok  = gah>=GA_MINIMO or gaa>=GA_MINIMO
    diffok = dp>=DIFF_POS_TRIPLE
    triple = o25ok and bttsok and defok and diffok

    if not o25ok: return None
    c1 = rmin>=RACHA_MIN_LAB; c3 = ravg>=RACHA_AVG_LAB and em

    if   triple and c1 and em: nivel="ELITE"
    elif triple and c3 and not c1: nivel="FUERTE"
    elif triple and c1: nivel="TRIPLE+"
    elif o25ok and defok: nivel="SEÑAL"
    else: return None

    info  = PROB[nivel]
    justo = round(info["over25"]/100, 2)
    return {
        "nivel":nivel,"badge":info["label"],"prob_over25":info["over25"],"poly_justo":justo,
        "fecha":fecha_str,"liga":liga,"home":home["nombre"],"away":away["nombre"],
        "home_id":home_id,"away_id":away_id,
        "pos_home":int(home["pos"]),"pos_away":int(away["pos"]),"diff_pos":dp,
        "racha_o25_h":roh,"racha_o25_a":roa,"racha_min":round(rmin,2),"racha_avg":round(ravg,2),
        "racha_btts_h":rbh,"racha_btts_a":rba,"ga_home":gah,"ga_away":gaa,
        "en_ene_may":"Sí" if em else "No","resultado":None,
    }

# ── JOB 1: Scanner cada 5 días ───────────────────────────────────────────────
def job_scanner():
    print(f"\n[{datetime.utcnow()}] 🔍 Iniciando scanner...")
    hoy=datetime.utcnow().date(); fin=hoy+timedelta(days=5)
    es_alta=hoy.month in MESES_ENE_MAY
    nuevas=[]

    for nombre, codigo in LIGAS.items():
        print(f"  {nombre}...", end=" ", flush=True)
        equipos=calcular_estado_liga(codigo); time.sleep(7)
        if not equipos: print("sin datos"); continue
        proximos=get_proximos_dias(codigo,5); time.sleep(7)
        hits=0
        for m in proximos:
            res=evaluar_partido(m["homeTeam"]["id"],m["awayTeam"]["id"],
                                m["utcDate"][:10],nombre,equipos)
            if res: nuevas.append(res); hits+=1
        print(f"{len(proximos)} próximos → {hits} alertas")

    if not nuevas:
        send_discord(f"📭 **Scanner {hoy} → {fin}**\nSin alertas para este período."); return

    agregar_alertas_pendientes(nuevas)
    orden={"ELITE":0,"TRIPLE+":1,"FUERTE":2,"SEÑAL":3}
    nuevas.sort(key=lambda x:(orden.get(x["nivel"],9),x["fecha"]))

    modo="🔥 ENE–MAY" if es_alta else "⭐ AGO–DIC"
    send_discord(f"# 🏟️ SCANNER OVER2.5 — {hoy} → {fin}\n**{len(nuevas)} alertas** | {modo}\n{'─'*40}")
    time.sleep(0.5)

    for a in nuevas:
        gan=round(a["poly_justo"]-0.50,2)
        send_discord(
            f"## {a['badge']}  `{a['fecha']}`  [{a['liga']}]\n"
            f"**{a['home']}** (#{a['pos_home']}) vs **{a['away']}** (#{a['pos_away']})\n"
            f"```\n"
            f"Racha O25  : {a['racha_o25_h']:.0%} / {a['racha_o25_a']:.0%}  (mín {a['racha_min']:.0%})\n"
            f"Racha BTTS : {a['racha_btts_h']:.0%} / {a['racha_btts_a']:.0%}\n"
            f"GA         : {a['ga_home']:.2f} / {a['ga_away']:.2f}  |  diff={a['diff_pos']}\n"
            f"Mes ene-may: {a['en_ene_may']}\n```\n"
            f"📊 Prob: **{a['prob_over25']}%**  |  Precio justo: **${a['poly_justo']:.2f}**\n"
            f"💰 Compras a $0.50 → **+${gan:.2f}** esperado"
        )
        time.sleep(0.8)

    # Tabla referencia
    tabla="## 📋 Referencia Polymarket\n```\n"
    for nivel in NIVELES_ORDEN:
        info=PROB[nivel]; j=round(info["over25"]/100,2); g=round(j-0.50,2)
        tabla+=f"{info['label']:<18} {info['over25']:>4}%   ${j:.2f}   +${g:.2f}\n"
    tabla+="```"
    send_discord(tabla); time.sleep(0.5)

    # Scoreboard si ya hay historial
    s=cargar_stats(); t=s["total"]
    if t["gana"]+t["pierde"]>0:
        send_discord(build_scoreboard(s,"*(Estado actual del tracker)*"))

    print(f"  ✅ {len(nuevas)} alertas enviadas.")

# ── JOB 2: Seguimiento diario ────────────────────────────────────────────────
def get_partidos_finalizados(liga_code, fecha_str):
    url=f"{BASE_URL}/competitions/{liga_code}/matches?dateFrom={fecha_str}&dateTo={fecha_str}&status=FINISHED"
    r=requests.get(url,headers=HEADERS,timeout=30)
    if r.status_code==429: time.sleep(65); r=requests.get(url,headers=HEADERS,timeout=30)
    if r.status_code!=200: return []
    return r.json().get("matches",[])

def job_seguimiento_diario():
    print(f"\n[{datetime.utcnow()}] 📊 Verificando resultados...")
    ayer=(datetime.utcnow().date()-timedelta(days=1)).isoformat()
    alertas=cargar_alertas()
    pendientes=[a for a in alertas if a["fecha"]==ayer and a["resultado"] is None]

    if not pendientes: print(f"  Sin pendientes para {ayer}."); return

    ligas_necesarias=list({a["liga"] for a in pendientes})
    resultados={}
    for liga in ligas_necesarias:
        codigo=LIGAS.get(liga)
        if not codigo: continue
        for p in get_partidos_finalizados(codigo,ayer):
            key=(p["homeTeam"]["name"],p["awayTeam"]["name"])
            gh=p["score"]["fullTime"]["home"]; ga=p["score"]["fullTime"]["away"]
            if gh is not None and ga is not None:
                resultados[key]={"gh":gh,"ga":ga,"total":gh+ga}
        time.sleep(7)

    resueltas=[]
    for a in alertas:
        if a["fecha"]!=ayer or a["resultado"] is not None: continue
        key=(a["home"],a["away"])
        if key in resultados:
            r=resultados[key]; es_over=r["total"]>2
            a["resultado"]="OVER ✅" if es_over else "UNDER ❌"
            a["goles_home"]=r["gh"]; a["goles_away"]=r["ga"]; a["total_goles"]=r["total"]
            resueltas.append((a,es_over))
            registrar_resultado_en_stats(a["nivel"],es_over)

    guardar_alertas(alertas)
    if not resueltas: print(f"  Sin resultados para {ayer}."); return

    aciertos=sum(1 for _,eo in resueltas if eo)
    total=len(resueltas)
    pct=round(aciertos/total*100) if total else 0

    send_discord(
        f"# 📊 RESULTADOS — {ayer}\n"
        f"**{aciertos}/{total} Over2.5 hoy** ({pct}%)\n{'─'*40}"
    )
    time.sleep(0.5)

    for a,es_over in resueltas:
        emoji="✅" if es_over else "❌"
        marcador=f"{a.get('goles_home','?')} - {a.get('goles_away','?')} ({a.get('total_goles','?')} goles)"
        send_discord(
            f"{emoji} **{a['badge']}** [{a['liga']}]\n"
            f"{a['home']} vs {a['away']}\n"
            f"Resultado: **{marcador}** → {a['resultado']}\n"
            f"*(Prob estimada: {a['prob_over25']}%)*"
        )
        time.sleep(0.6)

    # Scoreboard acumulado — se manda SIEMPRE al cerrar posiciones
    stats=cargar_stats()
    send_discord(build_scoreboard(
        stats,
        f"*Tras cerrar {total} posición{'es' if total>1 else ''} de {ayer}*"
    ))
    print(f"  ✅ {len(resueltas)} resultados procesados.")

# ── Servidor HTTP ────────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        alertas=cargar_alertas(); stats=cargar_stats(); t=stats["total"]
        body=json.dumps({
            "status":"running","data_dir":DATA_DIR,
            "alertas_totales":len(alertas),
            "pendientes":len([a for a in alertas if a["resultado"] is None]),
            "winrate_total":winrate(t["gana"],t["pierde"]),
            "stats":t,"timestamp":datetime.utcnow().isoformat(),
        }).encode()
        self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(body)
    def log_message(self,format,*args): pass

def run_server():
    port=int(os.environ.get("PORT",8080))
    HTTPServer(("0.0.0.0",port),HealthHandler).serve_forever()

# ── Arranque ─────────────────────────────────────────────────────────────────
if __name__=="__main__":
    print("="*55)
    print("  OVER2.5 BOT — Iniciando...")
    print(f"  Webhook : {'✅' if DISCORD_WEBHOOK_URL else '❌ falta DISCORD_WEBHOOK_URL'}")
    print(f"  Data dir: {DATA_DIR}")
    s=cargar_stats(); t=s["total"]
    print(f"  Stats   : {t['gana']}✅ {t['pierde']}❌  WR={winrate(t['gana'],t['pierde'])}")
    print("="*55)

    threading.Thread(target=run_server,daemon=True).start()
    scheduler=BackgroundScheduler(timezone="UTC")

    scheduler.add_job(job_scanner, trigger=IntervalTrigger(days=5), id="scanner",
                      next_run_time=datetime.utcnow()+timedelta(seconds=10))
    scheduler.add_job(job_seguimiento_diario, trigger=CronTrigger(hour=0,minute=30), id="seguimiento")

    scheduler.start()
    print("  → Scanner     : cada 5 días (primera vez en 10s)")
    print("  → Seguimiento : diario 00:30 UTC")

    try:
        while True: time.sleep(60)
    except (KeyboardInterrupt,SystemExit):
        scheduler.shutdown(); print("Bot detenido.")
