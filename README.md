# AInvestor

Bot de trading crypto personal con decisiones asistidas por IA (Composer 2.5 vía Cursor SDK), paper trading, gestión de riesgo determinista y dashboard de monitorización.

## Características

- **Paper trading** con ledger interno y precios reales (ccxt)
- **Ciclo IA horario** con Composer 2.5 Standard + fallback GPT-4o mini
- **Risk Manager** determinista (la IA propone, el código dispone)
- **Recolectores** de mercado, noticias (CryptoPanic) y sentimiento (Fear & Greed, Reddit)
- **Señales técnicas**: RSI, MA crossover, MACD, volumen
- **Scheduler** automático: mercado 15 min, riesgo 5 min, IA 60 min
- **Modos**: `paper` → `testnet` → `live` (capital limitado)
- **Backtesting** con métricas Sharpe, drawdown, profit factor
- **MCP server** para herramientas del agente Cursor

## Requisitos

- Python 3.12+
- Docker (opcional)
- API key de Cursor ([Dashboard → Integrations](https://cursor.com/dashboard/integrations))
- API key de OpenAI (fallback, opcional)

## Inicio rápido

```bash
# Clonar y configurar
cp .env.example .env
# Editar .env con tus API keys

# Instalar dependencias
pip install -e ".[dev]"

# Inicializar y arrancar
uvicorn ainvestor.main:app --reload --port 8000
```

Dashboard: http://localhost:8000

## Docker

```bash
docker compose up --build
```

## Scripts

```bash
# Ejecutar un ciclo IA manualmente
python scripts/run_cycle.py

# Backtest sobre histórico
python scripts/backtest.py --symbols BTC/USDT,ETH/USDT --days 90
```

## API

| Endpoint | Descripción |
|----------|-------------|
| `GET /` | Dashboard web |
| `GET /api/portfolio` | Estado del portfolio |
| `GET /api/trades` | Historial de operaciones |
| `GET /api/decisions` | Decisiones IA |
| `POST /api/cycle/run` | Ejecutar ciclo manual |
| `POST /api/kill-switch/on\|off` | Activar/desactivar kill switch |

## Arquitectura

```
Recolectores → QuantEngine → AIAgent (Composer 2.5) → RiskManager → Executor
```

La IA **nunca ejecuta órdenes directamente**. Todas las propuestas pasan por el Risk Manager.

## Modos de trading

| Modo | Variable `TRADING_MODE` | Descripción |
|------|-------------------------|-------------|
| Paper | `paper` | Simulador interno (por defecto) |
| Testnet | `testnet` | Binance testnet |
| Live | `live` | Dinero real (máx. `LIVE_MAX_CAPITAL_EUR`) |

## Configuración de riesgo

Editar `config/risk.yaml`: límites de posición, stop-loss, drawdown, whitelist de pares.

## Tests

```bash
pytest tests/ -v
```

## Aviso legal

Proyecto experimental con fines educativos. El trading de criptomonedas conlleva riesgo de pérdida total del capital. No constituye asesoramiento financiero.
