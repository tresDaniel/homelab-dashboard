# Homelab Dashboard

Dashboard web para monitorar serviços do homelab via Docker socket.

## Estrutura

```
homelab-dashboard/
├── main.py                      # Backend FastAPI + frontend inline
├── requirements.txt             # fastapi + uvicorn
├── homelab-dashboard.service   # Unit systemd
└── install.sh                  # Instalador automático
```

## Instalação rápida (Arch Linux)

```bash
sudo bash install.sh
```

O script:
1. Copia os arquivos para `/opt/homelab-dashboard`
2. Cria um virtualenv e instala dependências
3. Instala e habilita o serviço systemd
4. Garante que o usuário está no grupo `docker`

Acesse: **http://localhost:8000**

---

## Instalação manual

```bash
# 1. Instalar dependências
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn

# 2. Rodar
python main.py

# 3. (opcional) Porta e variáveis customizadas
PORT=9090 LLAMA_PORT=8080 python main.py
```

---

## Variáveis de ambiente

| Variável        | Padrão                  | Descrição                    |
|-----------------|-------------------------|------------------------------|
| `PORT`          | `8000`                  | Porta do servidor web        |
| `DOCKER_SOCKET` | `/var/run/docker.sock`  | Caminho do socket Docker     |
| `LLAMA_HOST`    | `localhost`             | Host do llama-server         |
| `LLAMA_PORT`    | `8080`                  | Porta do llama-server        |

---

## Endpoints da API

| Rota                | Descrição                              |
|---------------------|----------------------------------------|
| `GET /`             | Frontend HTML                          |
| `GET /api/containers` | Lista containers com CPU/MEM stats   |
| `GET /api/llama`    | Status do llama-server                 |
| `GET /api/docker/info` | Info do daemon Docker               |
| `GET /api/status`   | Health check da API                    |

---

## Permissões Docker

O usuário que roda o serviço precisa estar no grupo `docker`:

```bash
sudo usermod -aG docker $USER
# Faça logout/login para aplicar
```

Ou rode como root (não recomendado em produção).

---

## Comandos úteis

```bash
# Status
sudo systemctl status homelab-dashboard

# Logs ao vivo
sudo journalctl -u homelab-dashboard -f

# Reiniciar
sudo systemctl restart homelab-dashboard

# Parar
sudo systemctl stop homelab-dashboard
```
