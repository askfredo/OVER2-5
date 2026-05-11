# Over2.5 Bot — Railway + Discord

Scanner automático de señales Over2.5 con seguimiento diario de resultados.

## Qué hace
- **Cada 5 días**: escanea Premier, La Liga, Bundesliga, Serie A, Ligue1, Eredivisie, Primeira Liga y manda señales a Discord
- **Cada día a las 00:30 UTC**: verifica los partidos jugados ayer y reporta si fue Over o Under

## Deploy en Railway

### 1. Sube a GitHub
```bash
git init
git add .
git commit -m "first commit"
git remote add origin https://github.com/TU_USUARIO/over25-bot.git
git push -u origin main
```

### 2. Crea proyecto en Railway
- Entra a https://railway.app
- New Project → Deploy from GitHub repo
- Selecciona este repo

### 3. Variables de entorno en Railway
En tu proyecto Railway → Variables, agrega:

| Variable | Valor |
|---|---|
| `DISCORD_WEBHOOK_URL` | Tu webhook de Discord |
| `FOOTBALL_API_KEY` | Tu key de football-data.org (opcional) |

### Cómo obtener el webhook de Discord
1. En tu servidor Discord → canal → Editar canal → Integraciones → Webhooks
2. Nuevo Webhook → Copiar URL
3. Pega esa URL como valor de `DISCORD_WEBHOOK_URL`

## Archivos
- `main.py` — el bot completo
- `requirements.txt` — dependencias
- `Procfile` — instrucción de arranque para Railway
- `alertas_pendientes.json` — se crea solo, guarda alertas en seguimiento
