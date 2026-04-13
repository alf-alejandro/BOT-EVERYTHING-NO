# POLYBOT — Nothing Ever Happens Simulator

Bot de simulación para Polymarket que apuesta NO en mercados con >86% de probabilidad.

## Estructura

```
├── bot.py          # Lógica del bot (scanner + simulación)
├── app.py          # Servidor Flask + bot en background thread
├── requirements.txt
├── railway.toml    # Config Railway
└── templates/
    └── index.html  # Dashboard
```

## Deploy en Railway

1. Push este repo a GitHub
2. En Railway: **New Project → Deploy from GitHub repo**
3. Railway detecta automáticamente el `railway.toml`
4. ¡Listo! El dashboard queda en la URL que Railway asigna

## Variables de entorno (opcionales)

| Variable | Default | Descripción |
|---|---|---|
| `BOT_INTERVAL` | `300` | Segundos entre cada scan |
| `PORT` | `8080` | Puerto del dashboard (Railway lo setea automático) |

## Correr localmente

```bash
pip install -r requirements.txt
python app.py
# Dashboard en http://localhost:8080
```

## Parámetros del bot (en bot.py)

```python
NO_MIN_THRESHOLD  = 0.86   # Umbral mínimo de NO para entrar
NO_MAX_THRESHOLD  = 0.985  # Umbral máximo
MIN_VOLUME_USD    = 500    # Volumen mínimo del mercado
FIXED_ENTRY_USD   = 1.00   # Monto simulado por posición
MAX_POSITIONS     = 50     # Máximo de posiciones abiertas
LOST_CONFIRM_CHECKS  = 4   # Confirmaciones antes de marcar LOST
LOST_CONFIRM_DELAY_S = 8   # Segundos entre confirmaciones
```

> **Nota**: Railway tiene filesystem efímero — el CSV y state.json se resetean en cada deploy.
> Para persistencia real, migrar a PostgreSQL con la variable `DATABASE_URL`.
