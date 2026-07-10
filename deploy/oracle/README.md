# Despliegue AInvestor en Oracle Cloud (Always Free)

## 1. Crear cuenta y VM

Si Oracle devuelve *Out of capacity* en Madrid, programa reintentos automáticos a medianoche (hora España):

```powershell
# Si PowerShell bloquea scripts, usa el .cmd (evita cambiar políticas del sistema):
.\deploy\oracle\setup-oci-api.cmd

# O explícitamente:
powershell -ExecutionPolicy Bypass -File .\deploy\oracle\setup-oci-api.ps1

# Programar reintento esta noche a las 00:00
.\deploy\oracle\retry-midnight.cmd -RegisterTask
```

1. [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/)
2. Compute → Instances → Create instance
3. **Image:** Ubuntu 24.04 (o 22.04)
4. **Shape:** Ampere A1 (ARM) — Always Free eligible (1 OCPU, 6 GB RAM)
5. **Networking:** asignar IP pública
6. **SSH keys:** sube tu clave pública (`~/.ssh/id_ed25519.pub`)
7. Security List: permitir TCP **22** y **8000** desde tu IP (o 0.0.0.0/0 solo para pruebas)

## 2. Conectar por SSH

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@TU_IP_PUBLICA
```

## 3. Desplegar

```bash
curl -fsSL https://raw.githubusercontent.com/Alexfh94/AInvestor/main/deploy/oracle/setup.sh | bash
# o clonar y ejecutar:
git clone https://github.com/Alexfh94/AInvestor.git /opt/ainvestor
cd /opt/ainvestor
cp .env.example .env && nano .env
bash deploy/oracle/setup.sh
```

## 4. URL

`http://TU_IP_PUBLICA:8000`

## Seguridad

- Mantén `TRADING_MODE=paper`
- Restringe puerto 8000 en Security List a tu IP
- No expongas kill-switch sin autenticación en producción
