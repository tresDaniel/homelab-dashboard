# Homelab Dashboard

Dashboard web para monitorar serviços do homelab em Arch Linux.
Consulta o Docker daemon diretamente via Unix socket, lê sensores de hardware e calcula custo energético em tempo real.

## Funcionalidades

- **Containers Docker** — lista todos os containers com estado, CPU%, memória e link direto usando o IP local da LAN
- **llama-server** — verifica o health check do servidor de inferência nativo (fora do Docker)
- **Armazenamento** — mostra todas as partições reais com barra de uso e espaço livre/usado/total
- **Hardware & Energia** — temperatura de CPU e GPU, consumo em watts, custo em €/segundo, €/hora, €/dia e estimativa mensal
- **Info do sistema** — versão do Docker, kernel, arquitetura, RAM total, IP local da LAN
- Auto-refresh a cada 15 segundos

---

## Estrutura

```
homelab-dashboard/
├── main.py                     # Backend FastAPI + frontend HTML inline
├── requirements.txt            # fastapi + uvicorn + psutil
├── homelab-dashboard.service   # Unit systemd
├── install.sh                  # Instalador automático
└── README.md
```

---

## Dependências do sistema

Instale antes de rodar:

```bash
# Python e Docker (se ainda não tiver)
sudo pacman -S python docker

# Sensores de temperatura (necessário para leitura de CPU temp)
sudo pacman -S lm_sensors
sudo sensors-detect --auto   # detecta chips automaticamente
```

Para GPU **NVIDIA**, o `nvidia-smi` já vem com o driver. Para GPU **AMD**, instale o `rocm-smi` se quiser leitura de potência (temperatura funciona via hwmon mesmo sem ele).

---

## Instalação rápida

```bash
sudo bash install.sh
```

O script:
1. Verifica dependências (`python3`, `docker`, socket activo)
2. Copia os ficheiros para `/opt/homelab-dashboard`
3. Cria um virtualenv e instala `fastapi`, `uvicorn`, `psutil`
4. Instala e habilita o serviço systemd como o utilizador atual
5. Adiciona o utilizador ao grupo `docker` se necessário

Acesse em: **`http://<IP-LOCAL>:8000`**

---

## Instalação manual

```bash
# 1. Criar virtualenv e instalar dependências
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn psutil

# 2. Rodar
python main.py

# 3. Com variáveis de ambiente customizadas
PORT=9090 ENERGY_EUR_KWH=0.19 python main.py
```

---

## Variáveis de ambiente

| Variável           | Padrão                 | Descrição                                        |
|--------------------|------------------------|--------------------------------------------------|
| `PORT`             | `8000`                 | Porta do servidor web                            |
| `DOCKER_SOCKET`    | `/var/run/docker.sock` | Caminho do Unix socket do Docker daemon          |
| `LLAMA_HOST`       | `localhost`            | Host do llama-server                             |
| `LLAMA_PORT`       | `8080`                 | Porta do llama-server                            |
| `ENERGY_EUR_KWH`   | `0.22`                 | Tarifa de eletricidade em €/kWh — ajuste à sua   |

Exemplo com tarifa personalizada no systemd (`homelab-dashboard.service`):

```ini
Environment=ENERGY_EUR_KWH=0.19
```

---

## Endpoints da API

| Rota                  | Descrição                                                  |
|-----------------------|------------------------------------------------------------|
| `GET /`               | Frontend HTML (interface completa)                         |
| `GET /api/local-ip`   | IP local da máquina na LAN                                 |
| `GET /api/containers` | Lista containers Docker com estado, CPU% e memória         |
| `GET /api/docker/info`| Versão do Docker, kernel, OS, CPUs, RAM total, hostname    |
| `GET /api/disk`       | Partições montadas com uso, espaço livre e total em GB     |
| `GET /api/hardware`   | Temp CPU/GPU, watts, uso% e cálculo de custo energético    |
| `GET /api/llama`      | Health check do llama-server (`/health`)                   |
| `GET /api/status`     | Health check da própria API                                |

---

## Monitoramento de hardware

### Temperatura CPU
Lida em cascata:
1. `sensors -j` (lm-sensors) — prioriza chips `coretemp`, `k10temp`, `zenpower`
2. Fallback: `/sys/class/thermal/thermal_zone*/temp`

### Potência CPU (watts)
Lida via **Intel RAPL** (`/sys/class/powercap/intel-rapl/`). Faz duas amostras com 250ms de intervalo para calcular a potência instantânea. Funciona apenas em CPUs Intel. AMD não tem suporte nativo via RAPL no kernel (use `zenpower` ou `amdgpu` hwmon se disponível).

### GPU NVIDIA
Via `nvidia-smi`: temperatura, watts, limite de potência, utilização e VRAM usada/total.

### GPU AMD
Via `rocm-smi --json` (se instalado) ou fallback via `/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input`.

### Custo energético
Calculado como:

```
total_W = CPU_W + GPU_W + 30W (overhead placa-mãe estimado)
custo/h = (total_W / 1000) × ENERGY_EUR_KWH
```

Os 30W de overhead são uma estimativa conservadora. Ajuste diretamente no código se souber o consumo base real da sua placa-mãe.

---

## Permissões Docker

O utilizador que corre o serviço precisa de estar no grupo `docker`:

```bash
sudo usermod -aG docker $USER
# Faça logout/login para o grupo ter efeito
newgrp docker   # ou use este comando para aplicar na sessão atual
```

---

## Serviços Docker reconhecidos automaticamente

O dashboard identifica containers pelo nome e aplica ícone, descrição e porta padrão automaticamente:

| Serviço       | Tag      | Porta padrão |
|---------------|----------|--------------|
| Jellyfin      | MEDIA    | 8096         |
| Sonarr        | ARR      | 8989         |
| Radarr        | ARR      | 7878         |
| Bazarr        | ARR      | 6767         |
| Prowlarr      | ARR      | 9696         |
| FlareSolverr  | PROXY    | 8191         |
| Overseerr     | REQUEST  | 5055         |
| Jellyseerr    | REQUEST  | 5055         |
| Open WebUI    | AI       | 3000         |

Containers não reconhecidos aparecem como `📦 DOCKER` com a porta mapeada real (se houver).

---

## Comandos úteis

```bash
# Ver estado do serviço
sudo systemctl status homelab-dashboard

# Logs em tempo real
sudo journalctl -u homelab-dashboard -f

# Reiniciar após alterações
sudo systemctl restart homelab-dashboard

# Parar
sudo systemctl stop homelab-dashboard

# Desabilitar autostart
sudo systemctl disable homelab-dashboard
```

---

## Troubleshooting

**"Docker socket não encontrado"**
```bash
sudo systemctl start docker
sudo systemctl enable docker
```

**"Sem dados de temperatura"**
```bash
sudo sensors-detect --auto
sensors   # verificar se está a ler algum chip
```

**"psutil não instalado"**
```bash
source /opt/homelab-dashboard/venv/bin/activate
pip install psutil
sudo systemctl restart homelab-dashboard
```

**Porta 8000 já em uso**
```bash
# Alterar no ficheiro de serviço:
sudo systemctl edit homelab-dashboard
# Adicionar:
# [Service]
# Environment=PORT=8080
sudo systemctl restart homelab-dashboard
```
