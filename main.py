"""
Homelab Dashboard — Backend FastAPI
Consulta o Docker daemon via Unix socket e expõe uma API REST + serve o frontend.
"""

import asyncio
import json
import os
import socket
import time
import http.client
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


# ---------------------------------------------------------------------------
# Docker socket client (sem biblioteca externa, usa só stdlib)
# ---------------------------------------------------------------------------

DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
LLAMA_HOST = os.getenv("LLAMA_HOST", "localhost")
LLAMA_PORT = int(os.getenv("LLAMA_PORT", "8080"))


class DockerSocketClient:
    """Cliente HTTP leve que fala diretamente com o daemon Docker via Unix socket."""

    def __init__(self, socket_path: str = DOCKER_SOCKET):
        self.socket_path = socket_path

    def _request(self, method: str, path: str) -> dict:
        try:
            conn = http.client.HTTPConnection("localhost")
            # Substituir o socket TCP pelo Unix socket
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(self.socket_path)
            conn.sock = sock

            conn.request(method, path, headers={"Host": "localhost"})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            conn.close()

            if resp.status >= 400:
                return {"error": f"HTTP {resp.status}", "body": body}

            return json.loads(body) if body else {}
        except FileNotFoundError:
            return {"error": f"Docker socket não encontrado em {self.socket_path}"}
        except ConnectionRefusedError:
            return {"error": "Conexão recusada pelo Docker daemon"}
        except Exception as e:
            return {"error": str(e)}

    def containers(self, all_: bool = True) -> list:
        params = "?all=true" if all_ else ""
        result = self._request("GET", f"/containers/json{params}")
        if isinstance(result, list):
            return result
        return []

    def container_stats(self, container_id: str) -> dict:
        """Stats instantâneos (sem stream)."""
        return self._request("GET", f"/containers/{container_id}/stats?stream=false")

    def info(self) -> dict:
        return self._request("GET", "/info")

    def version(self) -> dict:
        return self._request("GET", "/version")


docker = DockerSocketClient()


# ---------------------------------------------------------------------------
# Mapeamento de serviços conhecidos
# ---------------------------------------------------------------------------

KNOWN_SERVICES = {
    "sonarr": {
        "icon": "📺",
        "desc": "Gerenciamento automático de séries de TV. Download e organização de episódios.",
        "tag": "ARR",
        "color": "#4fc4e8",
        "default_port": 8989,
    },
    "radarr": {
        "icon": "🎥",
        "desc": "Gerenciamento automático de filmes. Integração com indexadores e clientes torrent.",
        "tag": "ARR",
        "color": "#f5c518",
        "default_port": 7878,
    },
    "bazarr": {
        "icon": "💬",
        "desc": "Download automático de legendas. Integra com Sonarr e Radarr.",
        "tag": "ARR",
        "color": "#00ff88",
        "default_port": 6767,
    },
    "prowlarr": {
        "icon": "🔍",
        "desc": "Gerenciador de indexadores. Sincroniza com Sonarr, Radarr e outros.",
        "tag": "ARR",
        "color": "#ff6b35",
        "default_port": 9696,
    },
    "flaresolverr": {
        "icon": "🛡️",
        "desc": "Proxy para resolver desafios Cloudflare e DDoS-Guard.",
        "tag": "PROXY",
        "color": "#ff9f43",
        "default_port": 8191,
    },
    "jellyfin": {
        "icon": "🎬",
        "desc": "Servidor de mídia pessoal. Streaming de filmes, séries e músicas.",
        "tag": "MEDIA",
        "color": "#00d4ff",
        "default_port": 8096,
    },
    "open-webui": {
        "icon": "💡",
        "desc": "Interface web para modelos de linguagem. Conecta ao llama-server e APIs externas.",
        "tag": "AI",
        "color": "#c084fc",
        "default_port": 3000,
    },
    "overseerr": {
        "icon": "🎫",
        "desc": "Interface de pedidos de mídia. Integra com Jellyfin, Sonarr e Radarr.",
        "tag": "REQUEST",
        "color": "#a78bfa",
        "default_port": 5055,
    },
    "seer": {  # alias de overseerr / jellyseerr
        "icon": "🎫",
        "desc": "Interface de pedidos de mídia para usuários.",
        "tag": "REQUEST",
        "color": "#a78bfa",
        "default_port": 5055,
    },
    "jellyseerr": {
        "icon": "🎫",
        "desc": "Fork do Overseerr para Jellyfin. Gerencia pedidos de mídia.",
        "tag": "REQUEST",
        "color": "#a78bfa",
        "default_port": 5055,
    },
}


def _match_service(name: str) -> dict:
    """Tenta casar o nome do container com um serviço conhecido."""
    name_lower = name.lower().lstrip("/")
    for key, meta in KNOWN_SERVICES.items():
        if key in name_lower:
            return meta
    return {
        "icon": "📦",
        "desc": "Container Docker.",
        "tag": "DOCKER",
        "color": "#64748b",
        "default_port": None,
    }


def _extract_port(container: dict) -> Optional[int]:
    """Extrai a primeira porta pública mapeada do container."""
    ports = container.get("Ports", [])
    for p in ports:
        if p.get("PublicPort"):
            return p["PublicPort"]
    return None


def _parse_cpu_percent(stats: dict) -> float:
    """Calcula % CPU a partir das stats brutas do Docker."""
    try:
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"]
            - stats["precpu_stats"]["system_cpu_usage"]
        )
        num_cpus = stats["cpu_stats"].get("online_cpus") or len(
            stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])
        )
        if system_delta > 0:
            return round((cpu_delta / system_delta) * num_cpus * 100, 2)
    except (KeyError, ZeroDivisionError):
        pass
    return 0.0


def _parse_mem_mb(stats: dict) -> tuple[float, float]:
    """Retorna (uso_MB, limite_MB)."""
    try:
        mem = stats["memory_stats"]
        usage = (mem["usage"] - mem.get("stats", {}).get("cache", 0)) / 1024 / 1024
        limit = mem["limit"] / 1024 / 1024
        return round(usage, 1), round(limit, 1)
    except (KeyError, TypeError):
        return 0.0, 0.0


# ---------------------------------------------------------------------------
# Verificar llama-server
# ---------------------------------------------------------------------------

def check_llama_server() -> dict:
    try:
        conn = http.client.HTTPConnection(LLAMA_HOST, LLAMA_PORT, timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        data = json.loads(body) if body else {}
        return {
            "running": True,
            "status": data.get("status", "ok"),
            "http_status": resp.status,
        }
    except Exception as e:
        return {"running": False, "error": str(e)}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Homelab Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/containers")
def get_containers():
    """Retorna lista de containers com metadados e stats de CPU/memória."""
    raw = docker.containers(all_=True)
    result = []

    for c in raw:
        name = (c.get("Names") or ["/unknown"])[0].lstrip("/")
        meta = _match_service(name)
        state = c.get("State", "unknown")   # running | exited | paused | ...
        status_str = c.get("Status", "")
        port = _extract_port(c)

        # Busca stats apenas para containers rodando
        cpu_pct = 0.0
        mem_mb = 0.0
        mem_limit_mb = 0.0

        if state == "running":
            stats = docker.container_stats(c["Id"])
            if "error" not in stats:
                cpu_pct = _parse_cpu_percent(stats)
                mem_mb, mem_limit_mb = _parse_mem_mb(stats)

        result.append({
            "id": c["Id"][:12],
            "name": name,
            "image": c.get("Image", ""),
            "state": state,
            "status": status_str,
            "port": port or meta["default_port"],
            "icon": meta["icon"],
            "desc": meta["desc"],
            "tag": meta["tag"],
            "color": meta["color"],
            "cpu_percent": cpu_pct,
            "mem_mb": mem_mb,
            "mem_limit_mb": mem_limit_mb,
            "created": c.get("Created", 0),
            "uptime": status_str,
        })

    # Ordena: running primeiro, depois por nome
    result.sort(key=lambda x: (0 if x["state"] == "running" else 1, x["name"]))
    return result


@app.get("/api/llama")
def get_llama():
    """Verifica o llama-server."""
    return check_llama_server()


@app.get("/api/docker/info")
def get_docker_info():
    """Info geral do daemon Docker."""
    info = docker.info()
    ver = docker.version()
    if "error" in info:
        raise HTTPException(status_code=503, detail=info["error"])
    return {
        "containers_running": info.get("ContainersRunning", 0),
        "containers_stopped": info.get("ContainersStopped", 0),
        "containers_paused": info.get("ContainersPaused", 0),
        "images": info.get("Images", 0),
        "docker_version": ver.get("Version", "?"),
        "kernel": info.get("KernelVersion", "?"),
        "os": info.get("OperatingSystem", "?"),
        "arch": info.get("Architecture", "?"),
        "cpus": info.get("NCPU", 0),
        "mem_total_gb": round(info.get("MemTotal", 0) / 1024 / 1024 / 1024, 1),
        "hostname": info.get("Name", "?"),
    }


@app.get("/api/status")
def get_status():
    """Health check da própria API."""
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Serve o frontend (HTML inline — sem dependência de arquivos estáticos)
# ---------------------------------------------------------------------------

FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Homelab — Status</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0a0c10; --surface: #0f1218; --surface2: #151b24;
    --border: #1e2836; --border2: #243040;
    --accent: #00d4ff; --accent2: #00ff88; --accent3: #ff6b35; --accent4: #a78bfa;
    --text: #c8d8e8; --text-dim: #5a7080; --text-bright: #e8f4ff;
    --online: #00ff88; --offline: #ff4455; --warn: #ffaa00;
  }
  html, body { background: var(--bg); color: var(--text); font-family: 'IBM Plex Mono', monospace; font-size: 13px; line-height: 1.6; overflow-x: hidden; }
  body::after { content:''; position:fixed; inset:0; background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.025) 2px,rgba(0,0,0,0.025) 4px); pointer-events:none; z-index:9998; }
  .page-bg { position:fixed; inset:0; background-image:linear-gradient(rgba(0,212,255,0.018) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,0.018) 1px,transparent 1px); background-size:40px 40px; pointer-events:none; }
  .container { max-width:1240px; margin:0 auto; padding:36px 24px; position:relative; z-index:1; }

  /* Header */
  .header { display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:40px; padding-bottom:24px; border-bottom:1px solid var(--border); flex-wrap:wrap; gap:16px; }
  .prompt-line { color:var(--text-dim); font-size:11px; letter-spacing:.1em; display:flex; align-items:center; gap:8px; margin-bottom:4px; }
  .prompt-dot { width:6px; height:6px; background:var(--accent2); border-radius:50%; box-shadow:0 0 8px var(--accent2); animation:blink 2s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
  .header h1 { font-size:clamp(26px,4vw,40px); font-weight:600; color:var(--text-bright); letter-spacing:-.02em; line-height:1.1; }
  .header h1 span { color:var(--accent); text-shadow:0 0 20px rgba(0,212,255,.5); }
  .header-sub { color:var(--text-dim); font-size:11px; margin-top:4px; }
  .clock { font-size:24px; font-weight:300; color:var(--accent); text-shadow:0 0 18px rgba(0,212,255,.4); letter-spacing:.04em; }
  .date-str { font-size:11px; color:var(--text-dim); text-align:right; margin-top:4px; }

  /* Stat bar */
  .stats-bar { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:32px; }
  .stat-card { background:var(--surface); border:1px solid var(--border); padding:16px; position:relative; overflow:hidden; transition:border-color .2s; }
  .stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--c,var(--accent)); box-shadow:0 0 8px var(--c,var(--accent)); }
  .stat-label { font-size:10px; color:var(--text-dim); letter-spacing:.12em; text-transform:uppercase; margin-bottom:6px; }
  .stat-val { font-size:26px; font-weight:600; color:var(--c,var(--text-bright)); text-shadow:0 0 12px color-mix(in srgb, var(--c,#fff) 40%, transparent); line-height:1; }
  .stat-sub { font-size:10px; color:var(--text-dim); margin-top:4px; }

  /* Section */
  .section { margin-bottom:32px; }
  .section-header { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
  .section-title { font-size:11px; font-weight:500; color:var(--text-dim); letter-spacing:.15em; text-transform:uppercase; white-space:nowrap; }
  .section-line { flex:1; height:1px; background:var(--border); }
  .section-count { font-size:10px; color:var(--text-dim); background:var(--surface2); border:1px solid var(--border); padding:2px 8px; white-space:nowrap; }

  /* Services grid */
  .services-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:12px; }
  .svc-card { background:var(--surface); border:1px solid var(--border); padding:18px; position:relative; overflow:hidden; transition:all .25s cubic-bezier(.4,0,.2,1); display:flex; flex-direction:column; gap:12px; text-decoration:none; color:inherit; cursor:pointer; animation:fadeUp .4s ease both; }
  @keyframes fadeUp { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
  .svc-card:hover { border-color:var(--cc,var(--accent)); transform:translateY(-2px); box-shadow:0 8px 28px rgba(0,0,0,.5),0 0 0 1px var(--cc,var(--accent)),0 0 24px rgba(0,212,255,.04); }
  .svc-card::after { content:''; position:absolute; inset:0; background:linear-gradient(135deg,var(--cc,var(--accent)),transparent 60%); opacity:0; transition:opacity .3s; pointer-events:none; }
  .svc-card:hover::after { opacity:.04; }
  .svc-top { display:flex; align-items:center; justify-content:space-between; }
  .svc-icon { width:40px; height:40px; display:flex; align-items:center; justify-content:center; background:var(--surface2); border:1px solid var(--border2); font-size:18px; flex-shrink:0; }
  .badge { display:flex; align-items:center; gap:5px; font-size:10px; letter-spacing:.08em; padding:3px 8px; border:1px solid currentColor; }
  .badge.online { color:var(--online); border-color:rgba(0,255,136,.3); background:rgba(0,255,136,.05); }
  .badge.exited,.badge.offline { color:var(--offline); border-color:rgba(255,68,85,.3); background:rgba(255,68,85,.05); }
  .badge.paused { color:var(--warn); border-color:rgba(255,170,0,.3); background:rgba(255,170,0,.05); }
  .badge-dot { width:5px; height:5px; border-radius:50%; background:currentColor; box-shadow:0 0 6px currentColor; }
  .badge.online .badge-dot { animation:blink 2s infinite; }
  .svc-name { font-size:15px; font-weight:600; color:var(--text-bright); letter-spacing:-.01em; margin-bottom:2px; }
  .svc-desc { font-size:11px; color:var(--text-dim); line-height:1.5; }
  .svc-footer { display:flex; align-items:center; justify-content:space-between; border-top:1px solid var(--border); padding-top:10px; }
  .svc-tag { font-size:10px; color:var(--cc,var(--accent)); letter-spacing:.08em; opacity:.75; }
  .svc-info-right { display:flex; flex-direction:column; align-items:flex-end; gap:2px; }
  .svc-port { font-size:11px; color:var(--text-dim); }
  .svc-port b { color:var(--text-bright); }
  .svc-cpu-mem { font-size:10px; color:var(--text-dim); }
  .svc-uptime { font-size:10px; color:var(--text-dim); text-align:right; max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  /* LLM card */
  .llm-card { background:var(--surface); border:1px solid var(--border); padding:20px; position:relative; overflow:hidden; animation:fadeUp .4s ease .5s both; }
  .llm-card::before { content:''; position:absolute; top:0;left:0;right:0; height:2px; background:linear-gradient(90deg,var(--accent4),var(--accent),var(--accent2)); box-shadow:0 0 16px rgba(167,139,250,.4); }
  .llm-top { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:12px; }
  .llm-title { display:flex; align-items:center; gap:10px; }
  .llm-name { font-size:16px; font-weight:600; color:var(--text-bright); }
  .llm-native-badge { font-size:10px; color:var(--accent4); border:1px solid rgba(167,139,250,.3); background:rgba(167,139,250,.06); padding:2px 8px; letter-spacing:.08em; }
  .llm-body { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; border-top:1px solid var(--border); padding-top:12px; margin-top:12px; }
  .llm-meta { display:flex; flex-direction:column; gap:2px; }
  .llm-meta-label { font-size:10px; color:var(--text-dim); letter-spacing:.1em; text-transform:uppercase; }
  .llm-meta-value { font-size:13px; color:var(--text-bright); font-weight:500; }
  .open-btn { display:inline-flex; align-items:center; gap:4px; font-size:10px; color:var(--accent); text-decoration:none; letter-spacing:.05em; padding:4px 10px; border:1px solid rgba(0,212,255,.3); transition:all .2s; }
  .open-btn:hover { background:rgba(0,212,255,.06); border-color:var(--accent); }

  /* Docker info strip */
  .docker-strip { background:var(--surface); border:1px solid var(--border); padding:14px 20px; display:flex; flex-wrap:wrap; gap:24px; margin-bottom:32px; position:relative; }
  .docker-strip::before { content:''; position:absolute; top:0;left:0;right:0; height:2px; background:var(--accent3); box-shadow:0 0 8px var(--accent3); }
  .docker-info-item { display:flex; flex-direction:column; gap:2px; }
  .docker-info-label { font-size:10px; color:var(--text-dim); letter-spacing:.1em; text-transform:uppercase; }
  .docker-info-value { font-size:13px; color:var(--text-bright); font-weight:500; }

  /* Footer */
  .footer { margin-top:40px; padding-top:18px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
  .footer-left { font-size:11px; color:var(--text-dim); display:flex; align-items:center; gap:8px; }
  .footer-left::before { content:'//'; color:var(--accent); font-weight:600; }
  .footer-right { font-size:10px; color:var(--text-dim); }
  .refresh-btn { font-size:10px; color:var(--accent); background:none; border:1px solid rgba(0,212,255,.3); padding:4px 12px; cursor:pointer; font-family:inherit; letter-spacing:.08em; transition:all .2s; }
  .refresh-btn:hover { background:rgba(0,212,255,.06); }
  .spinner { display:inline-block; width:10px; height:10px; border:1px solid rgba(0,212,255,.3); border-top-color:var(--accent); border-radius:50%; animation:spin .6s linear infinite; margin-left:6px; vertical-align:middle; }
  @keyframes spin { to { transform:rotate(360deg) } }
  .error-msg { color:var(--offline); font-size:11px; padding:12px; border:1px solid rgba(255,68,85,.2); background:rgba(255,68,85,.04); margin-top:8px; }
  @media(max-width:600px){ .services-grid{grid-template-columns:1fr} .stats-bar{grid-template-columns:repeat(2,1fr)} }
</style>
</head>
<body>
<div class="page-bg"></div>
<div class="container">

  <header class="header">
    <div>
      <div class="prompt-line"><div class="prompt-dot"></div> arch linux · homelab · docker</div>
      <h1>home<span>lab</span></h1>
      <div class="header-sub" id="hostname">carregando...</div>
    </div>
    <div style="text-align:right">
      <div class="clock" id="clock">--:--:--</div>
      <div class="date-str" id="datestr">---</div>
    </div>
  </header>

  <!-- Stats bar -->
  <div class="stats-bar">
    <div class="stat-card" style="--c:var(--online)">
      <div class="stat-label">Containers Online</div>
      <div class="stat-val" id="stat-online">—</div>
      <div class="stat-sub" id="stat-total">de — total</div>
    </div>
    <div class="stat-card" style="--c:var(--accent)">
      <div class="stat-label">Imagens Docker</div>
      <div class="stat-val" id="stat-images">—</div>
      <div class="stat-sub">em cache local</div>
    </div>
    <div class="stat-card" style="--c:#38bdf8">
      <div class="stat-label">CPU Cores</div>
      <div class="stat-val" id="stat-cpus">—</div>
      <div class="stat-sub" id="stat-arch">—</div>
    </div>
    <div class="stat-card" style="--c:var(--accent4)">
      <div class="stat-label">Memória Total</div>
      <div class="stat-val" id="stat-mem">—</div>
      <div class="stat-sub">GB disponíveis</div>
    </div>
  </div>

  <!-- Docker info strip -->
  <div class="docker-strip" id="docker-strip" style="display:none">
    <div class="docker-info-item"><div class="docker-info-label">Docker</div><div class="docker-info-value" id="di-version">—</div></div>
    <div class="docker-info-item"><div class="docker-info-label">Kernel</div><div class="docker-info-value" id="di-kernel">—</div></div>
    <div class="docker-info-item"><div class="docker-info-label">Sistema</div><div class="docker-info-value" id="di-os">—</div></div>
    <div class="docker-info-item"><div class="docker-info-label">Arquitetura</div><div class="docker-info-value" id="di-arch">—</div></div>
    <div class="docker-info-item"><div class="docker-info-label">Host</div><div class="docker-info-value" id="di-host">—</div></div>
  </div>

  <!-- Containers -->
  <div class="section">
    <div class="section-header">
      <div class="section-title">Containers Docker</div>
      <div class="section-line"></div>
      <div class="section-count" id="svc-count">carregando…</div>
    </div>
    <div id="services-grid" class="services-grid"></div>
    <div id="svc-error" class="error-msg" style="display:none"></div>
  </div>

  <!-- LLM -->
  <div class="section">
    <div class="section-header">
      <div class="section-title">Servidor LLM</div>
      <div class="section-line"></div>
      <div class="section-count">1 instância</div>
    </div>
    <div class="llm-card">
      <div class="llm-top">
        <div class="llm-title">
          <span style="font-size:22px">🤖</span>
          <div class="llm-name">llama-server</div>
          <div class="llm-native-badge">NATIVO</div>
          <div class="badge" id="llm-badge" style="color:var(--warn);border-color:rgba(255,170,0,.3);background:rgba(255,170,0,.05)">
            <div class="badge-dot"></div><span>AGUARDANDO</span>
          </div>
        </div>
        <a href="http://localhost:8080" target="_blank" class="open-btn">↗ abrir</a>
      </div>
      <div style="font-size:12px;color:var(--text-dim);line-height:1.6">
        Servidor de inferência local via llama.cpp. Expõe API compatível com OpenAI em
        <span style="color:var(--accent4)">:8080</span>. Roda fora do Docker, direto no host.
      </div>
      <div class="llm-body">
        <div class="llm-meta"><div class="llm-meta-label">Backend</div><div class="llm-meta-value">llama.cpp</div></div>
        <div class="llm-meta"><div class="llm-meta-label">Porta</div><div class="llm-meta-value">8080</div></div>
        <div class="llm-meta"><div class="llm-meta-label">API</div><div class="llm-meta-value">OpenAI-compat.</div></div>
        <div class="llm-meta"><div class="llm-meta-label">Status</div><div class="llm-meta-value" id="llm-status-text">verificando…</div></div>
      </div>
    </div>
  </div>

  <footer class="footer">
    <div class="footer-left">homelab dashboard · arch linux</div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="footer-right">atualizado: <span id="last-refresh">—</span></div>
      <button class="refresh-btn" onclick="loadAll()">↻ atualizar</button>
    </div>
  </footer>
</div>

<script>
// Clock
function tick() {
  const now = new Date();
  const p = n => String(n).padStart(2,'0');
  document.getElementById('clock').textContent = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
  const days = ['Dom','Seg','Ter','Qua','Qui','Sex','Sáb'];
  const months = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
  document.getElementById('datestr').textContent = `${days[now.getDay()]}, ${p(now.getDate())} ${months[now.getMonth()]} ${now.getFullYear()}`;
}
setInterval(tick, 1000); tick();

function stateLabel(state) {
  return { running: 'ONLINE', exited: 'OFFLINE', paused: 'PAUSADO', restarting: 'RESTART' }[state] || state.toUpperCase();
}

function stateClass(state) {
  return { running: 'online', exited: 'exited', paused: 'paused' }[state] || 'exited';
}

function renderServices(services) {
  const grid = document.getElementById('services-grid');
  document.getElementById('svc-count').textContent = `${services.length} containers`;
  document.getElementById('stat-online').textContent = services.filter(s => s.state === 'running').length;
  document.getElementById('stat-total').textContent = `de ${services.length} total`;

  grid.innerHTML = services.map((s, i) => {
    const href = s.port ? `http://${location.hostname}:${s.port}` : '#';
    const delay = `animation-delay:${0.05 + i * 0.045}s`;
    const cpuMem = s.state === 'running'
      ? `CPU ${s.cpu_percent}% · MEM ${s.mem_mb} MB`
      : '';
    return `
    <a class="svc-card" href="${href}" target="${s.port ? '_blank' : '_self'}" style="--cc:${s.color};${delay}" ${!s.port ? 'onclick="return false"' : ''}>
      <div class="svc-top">
        <div class="svc-icon">${s.icon}</div>
        <div class="badge ${stateClass(s.state)}">
          <div class="badge-dot"></div>
          <span>${stateLabel(s.state)}</span>
        </div>
      </div>
      <div>
        <div class="svc-name">${s.name}</div>
        <div class="svc-desc">${s.desc}</div>
      </div>
      <div class="svc-footer">
        <div class="svc-tag">${s.tag}</div>
        <div class="svc-info-right">
          ${s.port ? `<div class="svc-port">:<b>${s.port}</b></div>` : ''}
          ${cpuMem ? `<div class="svc-cpu-mem">${cpuMem}</div>` : ''}
          <div class="svc-uptime" title="${s.uptime}">${s.uptime}</div>
        </div>
      </div>
    </a>`;
  }).join('');
}

async function loadDockerInfo() {
  try {
    const r = await fetch('/api/docker/info');
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('stat-images').textContent = d.images;
    document.getElementById('stat-cpus').textContent = d.cpus;
    document.getElementById('stat-arch').textContent = d.arch;
    document.getElementById('stat-mem').textContent = d.mem_total_gb;
    document.getElementById('hostname').textContent = `${d.hostname} · ${d.os}`;
    document.getElementById('di-version').textContent = d.docker_version;
    document.getElementById('di-kernel').textContent = d.kernel;
    document.getElementById('di-os').textContent = d.os;
    document.getElementById('di-arch').textContent = d.arch;
    document.getElementById('di-host').textContent = d.hostname;
    document.getElementById('docker-strip').style.display = 'flex';
  } catch(e) {}
}

async function loadContainers() {
  const errEl = document.getElementById('svc-error');
  try {
    const r = await fetch('/api/containers');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    errEl.style.display = 'none';
    renderServices(data);
  } catch(e) {
    errEl.style.display = 'block';
    errEl.textContent = `Erro ao consultar Docker: ${e.message}. Verifique se o backend tem acesso ao socket /var/run/docker.sock`;
  }
}

async function loadLlama() {
  try {
    const r = await fetch('/api/llama');
    const d = await r.json();
    const badge = document.getElementById('llm-badge');
    const statusText = document.getElementById('llm-status-text');
    if (d.running) {
      badge.className = 'badge online';
      badge.innerHTML = '<div class="badge-dot"></div><span>ONLINE</span>';
      statusText.textContent = d.status || 'ok';
      statusText.style.color = 'var(--online)';
    } else {
      badge.className = 'badge exited';
      badge.innerHTML = '<div class="badge-dot"></div><span>OFFLINE</span>';
      statusText.textContent = 'inacessível';
      statusText.style.color = 'var(--offline)';
    }
  } catch(e) {}
}

async function loadAll() {
  await Promise.all([loadDockerInfo(), loadContainers(), loadLlama()]);
  const now = new Date();
  const p = n => String(n).padStart(2,'0');
  document.getElementById('last-refresh').textContent =
    `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
}

loadAll();
setInterval(loadAll, 15000); // atualiza a cada 15s
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(content=FRONTEND_HTML)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
