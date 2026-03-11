"""
Homelab Dashboard — Backend FastAPI
- Docker daemon via Unix socket
- IP local detectado automaticamente (links usam IP real da LAN)
- Info de disco (psutil)
- Monitor de hardware: CPU/GPU temp, watts, custo energético (€)
"""

import json
import os
import socket
import subprocess
import http.client
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------
DOCKER_SOCKET  = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
LLAMA_HOST     = os.getenv("LLAMA_HOST", "localhost")
LLAMA_PORT     = int(os.getenv("LLAMA_PORT", "8080"))
ENERGY_EUR_KWH = float(os.getenv("ENERGY_EUR_KWH", "0.14"))  # €/kWh — ajuste para a tarifa

# ---------------------------------------------------------------------------
# Helpers: IP local
# ---------------------------------------------------------------------------

def get_local_ip() -> str:
    """Devolve o IP da interface de rede primária (ex: 192.168.1.x)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()

# ---------------------------------------------------------------------------
# Docker socket client
# ---------------------------------------------------------------------------

class DockerSocketClient:
    def __init__(self, socket_path: str = DOCKER_SOCKET):
        self.socket_path = socket_path

    def _request(self, method: str, path: str):
        try:
            conn = http.client.HTTPConnection("localhost")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(self.socket_path)
            conn.sock = sock
            conn.request(method, path, headers={"Host": "localhost"})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            conn.close()
            if resp.status >= 400:
                return {"error": f"HTTP {resp.status}"}
            return json.loads(body) if body else {}
        except FileNotFoundError:
            return {"error": f"Socket não encontrado: {self.socket_path}"}
        except ConnectionRefusedError:
            return {"error": "Docker daemon recusou conexão"}
        except Exception as e:
            return {"error": str(e)}

    def containers(self, all_: bool = True) -> list:
        r = self._request("GET", f"/containers/json{'?all=true' if all_ else ''}")
        return r if isinstance(r, list) else []

    def container_stats(self, cid: str) -> dict:
        r = self._request("GET", f"/containers/{cid}/stats?stream=false")
        return r if isinstance(r, dict) else {}

    def info(self) -> dict:
        r = self._request("GET", "/info")
        return r if isinstance(r, dict) else {}

    def version(self) -> dict:
        r = self._request("GET", "/version")
        return r if isinstance(r, dict) else {}

docker = DockerSocketClient()

# ---------------------------------------------------------------------------
# Mapeamento de serviços conhecidos
# ---------------------------------------------------------------------------

KNOWN_SERVICES = {
    "sonarr":       {"icon": "📺", "desc": "Gerenciamento automático de séries de TV.",           "tag": "ARR",     "color": "#4fc4e8", "default_port": 8989},
    "radarr":       {"icon": "🎥", "desc": "Gerenciamento automático de filmes.",                  "tag": "ARR",     "color": "#f5c518", "default_port": 7878},
    "bazarr":       {"icon": "💬", "desc": "Download automático de legendas para mídia.",          "tag": "ARR",     "color": "#00ff88", "default_port": 6767},
    "prowlarr":     {"icon": "🔍", "desc": "Gerenciador de indexadores e trackers.",               "tag": "ARR",     "color": "#ff6b35", "default_port": 9696},
    "flaresolverr": {"icon": "🛡️", "desc": "Proxy para resolver desafios Cloudflare/DDoS-Guard.", "tag": "PROXY",   "color": "#ff9f43", "default_port": 8191},
    "jellyfin":     {"icon": "🎬", "desc": "Servidor de mídia pessoal. Streaming local.",          "tag": "MEDIA",   "color": "#00d4ff", "default_port": 8096},
    "open-webui":   {"icon": "💡", "desc": "Interface web para LLMs. Conecta ao llama-server.",   "tag": "AI",      "color": "#c084fc", "default_port": 3000},
    "overseerr":    {"icon": "🎫", "desc": "Interface de pedidos de mídia.",                       "tag": "REQUEST", "color": "#a78bfa", "default_port": 5055},
    "jellyseerr":   {"icon": "🎫", "desc": "Fork do Overseerr para Jellyfin.",                     "tag": "REQUEST", "color": "#a78bfa", "default_port": 5055},
    "seer":         {"icon": "🎫", "desc": "Interface de pedidos de mídia.",                       "tag": "REQUEST", "color": "#a78bfa", "default_port": 5055},
}

def _match_service(name: str) -> dict:
    n = name.lower().lstrip("/")
    for key, meta in KNOWN_SERVICES.items():
        if key in n:
            return meta
    return {"icon": "📦", "desc": "Container Docker.", "tag": "DOCKER", "color": "#64748b", "default_port": None}

def _extract_port(container: dict) -> Optional[int]:
    for p in container.get("Ports", []):
        if p.get("PublicPort"):
            return p["PublicPort"]
    return None

def _parse_cpu_percent(stats: dict) -> float:
    try:
        cd = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        sd = stats["cpu_stats"]["system_cpu_usage"]         - stats["precpu_stats"]["system_cpu_usage"]
        nc = stats["cpu_stats"].get("online_cpus") or len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
        return round((cd / sd) * nc * 100, 2) if sd > 0 else 0.0
    except (KeyError, ZeroDivisionError):
        return 0.0

def _parse_mem_mb(stats: dict):
    try:
        m = stats["memory_stats"]
        usage = (m["usage"] - m.get("stats", {}).get("cache", 0)) / 1024 / 1024
        return round(usage, 1), round(m["limit"] / 1024 / 1024, 1)
    except (KeyError, TypeError):
        return 0.0, 0.0

# ---------------------------------------------------------------------------
# Disco (psutil)
# ---------------------------------------------------------------------------

IGNORED_FS = {
    "tmpfs","devtmpfs","squashfs","overlay","devpts","sysfs","proc",
    "cgroup","cgroup2","pstore","bpf","tracefs","debugfs","hugetlbfs",
    "mqueue","configfs","fusectl","fuse.portal","ramfs","efivarfs",
}

def get_disk_info() -> list:
    try:
        import psutil
        seen, result = set(), []
        for p in psutil.disk_partitions(all=False):
            if p.device in seen or p.fstype in IGNORED_FS:
                continue
            seen.add(p.device)
            try:
                u = psutil.disk_usage(p.mountpoint)
                result.append({
                    "device":     p.device,
                    "mountpoint": p.mountpoint,
                    "fstype":     p.fstype,
                    "total_gb":   round(u.total  / 1024**3, 1),
                    "used_gb":    round(u.used   / 1024**3, 1),
                    "free_gb":    round(u.free   / 1024**3, 1),
                    "percent":    u.percent,
                })
            except PermissionError:
                pass
        return result
    except ImportError:
        return [{"error": "psutil não instalado — pip install psutil"}]

# ---------------------------------------------------------------------------
# Hardware: temperatura, potência (watts), custo energético
# ---------------------------------------------------------------------------

def _run(cmd: list, timeout: int = 3) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def _cpu_temp_sensors() -> Optional[float]:
    """Lê temperatura via `sensors -j` (lm-sensors)."""
    out = _run(["sensors", "-j"])
    if not out:
        return None
    try:
        data = json.loads(out)
        best = []
        for chip, features in data.items():
            if not isinstance(features, dict):
                continue
            for feat_name, feat_val in features.items():
                if not isinstance(feat_val, dict):
                    continue
                for k, v in feat_val.items():
                    if "input" in k and isinstance(v, (int, float)) and 20 < v < 120:
                        tier = 1 if any(x in chip.lower() for x in ("coretemp","k10temp","zenpower")) else 2
                        best.append((tier, v))
        if best:
            best.sort()
            tier0 = best[0][0]
            vals = [v for t, v in best if t == tier0]
            return round(sum(vals) / len(vals), 1)
    except Exception:
        pass
    return None

def _cpu_temp_thermal() -> Optional[float]:
    """Fallback: /sys/class/thermal/thermal_zone*/temp"""
    try:
        import glob
        temps = []
        for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                t = int(open(z).read().strip()) / 1000
                if 20 < t < 120:
                    temps.append(t)
            except Exception:
                pass
        return round(sum(temps) / len(temps), 1) if temps else None
    except Exception:
        return None

def _gpu_info_nvidia() -> dict:
    out = _run(["nvidia-smi",
                "--query-gpu=temperature.gpu,power.draw,power.limit,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits"])
    if not out:
        return {}
    try:
        p = [x.strip() for x in out.split(",")]
        return {
            "name":          p[3] if len(p) > 3 else "NVIDIA GPU",
            "temp_c":        float(p[0]),
            "power_w":       float(p[1]),
            "power_limit_w": float(p[2]),
            "util_pct":      float(p[4]) if len(p) > 4 else None,
            "vram_used_mb":  float(p[5]) if len(p) > 5 else None,
            "vram_total_mb": float(p[6]) if len(p) > 6 else None,
        }
    except Exception:
        return {}

def _gpu_info_amd() -> dict:
    # rocm-smi
    out = _run(["rocm-smi", "--showtemp", "--showpower", "--json"])
    if out:
        try:
            data = json.loads(out)
            card = next(iter(data.values()))
            return {
                "name":    "AMD GPU",
                "temp_c":  float(card.get("Temperature (Sensor edge) (C)", 0)),
                "power_w": float(card.get("Average Graphics Package Power (W)", 0)),
            }
        except Exception:
            pass
    # hwmon fallback
    try:
        import glob
        for path in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"):
            t = int(open(path).read().strip()) / 1000
            if 20 < t < 120:
                return {"name": "AMD GPU", "temp_c": round(t, 1), "power_w": None}
    except Exception:
        pass
    return {}

def _cpu_power_rapl() -> Optional[float]:
    """Intel RAPL — mede energia em ~250ms."""
    try:
        import glob, time
        paths = sorted(glob.glob("/sys/class/powercap/intel-rapl/intel-rapl:*/energy_uj"))
        if not paths:
            return None
        def read():
            return sum(int(open(p).read()) for p in paths)
        e1, t1 = read(), time.monotonic()
        time.sleep(0.25)
        e2, t2 = read(), time.monotonic()
        return round((e2 - e1) / 1e6 / (t2 - t1), 1)
    except Exception:
        return None

def _cpu_usage_proc() -> Optional[float]:
    try:
        import time
        def stat():
            vals = list(map(int, open("/proc/stat").readline().split()[1:]))
            idle  = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return idle, sum(vals)
        i1, t1 = stat()
        time.sleep(0.2)
        i2, t2 = stat()
        dt = t2 - t1
        return round((1 - (i2-i1)/dt) * 100, 1) if dt > 0 else None
    except Exception:
        return None

def get_hardware_info() -> dict:
    cpu_temp  = _cpu_temp_sensors() or _cpu_temp_thermal()
    cpu_power = _cpu_power_rapl()
    cpu_usage = _cpu_usage_proc()
    gpu       = _gpu_info_nvidia() or _gpu_info_amd()

    breakdown: dict = {}
    if cpu_power is not None:
        breakdown["cpu_w"] = cpu_power
    if gpu.get("power_w"):
        breakdown["gpu_w"] = float(gpu["power_w"])

    total_w = round(sum(breakdown.values()) + 30, 1) if breakdown else None  # +30W overhead

    cost: dict = {}
    if total_w is not None:
        kw = total_w / 1000
        cost = {
            "per_second_eur": round(kw * ENERGY_EUR_KWH / 3600, 8),
            "per_hour_eur":   round(kw * ENERGY_EUR_KWH, 4),
            "per_day_eur":    round(kw * ENERGY_EUR_KWH * 24, 3),
            "per_month_eur":  round(kw * ENERGY_EUR_KWH * 24 * 30, 2),
            "eur_kwh_rate":   ENERGY_EUR_KWH,
        }

    return {
        "cpu":       {"temp_c": cpu_temp, "power_w": cpu_power, "usage_pct": cpu_usage},
        "gpu":       gpu if gpu else None,
        "total_w":   total_w,
        "breakdown": breakdown,
        "cost":      cost,
        "energy_rate_eur_kwh": ENERGY_EUR_KWH,
    }

# ---------------------------------------------------------------------------
# llama-server
# ---------------------------------------------------------------------------

def check_llama() -> dict:
    try:
        conn = http.client.HTTPConnection(LLAMA_HOST, LLAMA_PORT, timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        data = json.loads(body) if body else {}
        return {"running": True, "status": data.get("status", "ok")}
    except Exception as e:
        return {"running": False, "error": str(e)}

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Homelab Dashboard", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.get("/api/local-ip")
def api_local_ip():
    return {"ip": LOCAL_IP}


@app.get("/api/containers")
def api_containers():
    raw = docker.containers(all_=True)
    result = []
    for c in raw:
        name  = (c.get("Names") or ["/unknown"])[0].lstrip("/")
        meta  = _match_service(name)
        state = c.get("State", "unknown")
        port  = _extract_port(c)
        cpu_pct, mem_mb, mem_limit = 0.0, 0.0, 0.0
        if state == "running":
            stats = docker.container_stats(c["Id"])
            if "error" not in stats:
                cpu_pct = _parse_cpu_percent(stats)
                mem_mb, mem_limit = _parse_mem_mb(stats)
        result.append({
            "id": c["Id"][:12], "name": name, "image": c.get("Image",""),
            "state": state, "status": c.get("Status",""),
            "port": port or meta["default_port"],
            "icon": meta["icon"], "desc": meta["desc"],
            "tag": meta["tag"],  "color": meta["color"],
            "cpu_percent": cpu_pct, "mem_mb": mem_mb, "mem_limit_mb": mem_limit,
        })
    result.sort(key=lambda x: (0 if x["state"]=="running" else 1, x["name"]))
    return result


@app.get("/api/docker/info")
def api_docker_info():
    info = docker.info()
    ver  = docker.version()
    if "error" in info:
        raise HTTPException(503, detail=info["error"])
    return {
        "containers_running": info.get("ContainersRunning", 0),
        "containers_stopped": info.get("ContainersStopped", 0),
        "images":             info.get("Images", 0),
        "docker_version":     ver.get("Version", "?"),
        "kernel":             info.get("KernelVersion", "?"),
        "os":                 info.get("OperatingSystem", "?"),
        "arch":               info.get("Architecture", "?"),
        "cpus":               info.get("NCPU", 0),
        "mem_total_gb":       round(info.get("MemTotal", 0) / 1024**3, 1),
        "hostname":           info.get("Name", "?"),
    }


@app.get("/api/disk")
def api_disk():
    return get_disk_info()


@app.get("/api/hardware")
def api_hardware():
    return get_hardware_info()


@app.get("/api/llama")
def api_llama():
    return check_llama()


@app.get("/api/status")
def api_status():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat(), "local_ip": LOCAL_IP}


# ---------------------------------------------------------------------------
# Frontend HTML (inline, sem arquivos estáticos)
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
    --bg:#0a0c10; --surface:#0f1218; --surface2:#151b24;
    --border:#1e2836; --border2:#243040;
    --accent:#00d4ff; --accent2:#00ff88; --accent3:#ff6b35; --accent4:#a78bfa;
    --text:#c8d8e8; --text-dim:#5a7080; --text-bright:#e8f4ff;
    --online:#00ff88; --offline:#ff4455; --warn:#ffaa00;
  }
  html,body { background:var(--bg); color:var(--text); font-family:'IBM Plex Mono',monospace; font-size:13px; line-height:1.6; overflow-x:hidden; }
  body::after { content:''; position:fixed; inset:0; background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.025) 2px,rgba(0,0,0,.025) 4px); pointer-events:none; z-index:9998; }
  .grid-bg { position:fixed; inset:0; background-image:linear-gradient(rgba(0,212,255,.016) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.016) 1px,transparent 1px); background-size:40px 40px; pointer-events:none; }
  .wrap { max-width:1280px; margin:0 auto; padding:36px 24px; position:relative; z-index:1; }

  .header { display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:36px; padding-bottom:22px; border-bottom:1px solid var(--border); flex-wrap:wrap; gap:16px; }
  .prompt-line { color:var(--text-dim); font-size:11px; letter-spacing:.1em; display:flex; align-items:center; gap:8px; margin-bottom:4px; }
  .pdot { width:6px; height:6px; background:var(--accent2); border-radius:50%; box-shadow:0 0 8px var(--accent2); animation:blink 2s infinite; }
  @keyframes blink { 0%,100%{opacity:1}50%{opacity:.3} }
  h1 { font-size:clamp(26px,3.5vw,40px); font-weight:600; color:var(--text-bright); letter-spacing:-.02em; line-height:1.1; }
  h1 span { color:var(--accent); text-shadow:0 0 20px rgba(0,212,255,.5); }
  .hd-sub { color:var(--text-dim); font-size:11px; margin-top:4px; }
  .clock { font-size:24px; font-weight:300; color:var(--accent); text-shadow:0 0 18px rgba(0,212,255,.4); letter-spacing:.04em; }
  .datestr { font-size:11px; color:var(--text-dim); text-align:right; margin-top:4px; }

  .stats-bar { display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); gap:10px; margin-bottom:28px; }
  .sc { background:var(--surface); border:1px solid var(--border); padding:14px 16px; position:relative; overflow:hidden; }
  .sc::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--c,var(--accent)); box-shadow:0 0 8px var(--c,var(--accent)); }
  .sc-label { font-size:10px; color:var(--text-dim); letter-spacing:.12em; text-transform:uppercase; margin-bottom:6px; }
  .sc-val { font-size:24px; font-weight:600; color:var(--c,var(--text-bright)); line-height:1; }
  .sc-sub { font-size:10px; color:var(--text-dim); margin-top:4px; }

  .section { margin-bottom:28px; }
  .sec-head { display:flex; align-items:center; gap:10px; margin-bottom:12px; }
  .sec-title { font-size:11px; font-weight:500; color:var(--text-dim); letter-spacing:.15em; text-transform:uppercase; white-space:nowrap; }
  .sec-line { flex:1; height:1px; background:var(--border); }
  .sec-count { font-size:10px; color:var(--text-dim); background:var(--surface2); border:1px solid var(--border); padding:2px 8px; }

  .docker-strip { background:var(--surface); border:1px solid var(--border); padding:12px 18px; display:flex; flex-wrap:wrap; gap:20px; margin-bottom:28px; position:relative; }
  .docker-strip::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--accent3); box-shadow:0 0 8px var(--accent3); }
  .di { display:flex; flex-direction:column; gap:2px; }
  .di-label { font-size:10px; color:var(--text-dim); letter-spacing:.1em; text-transform:uppercase; }
  .di-value { font-size:13px; color:var(--text-bright); font-weight:500; }

  .services-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:10px; }
  .svc { background:var(--surface); border:1px solid var(--border); padding:16px; position:relative; overflow:hidden; transition:all .22s cubic-bezier(.4,0,.2,1); display:flex; flex-direction:column; gap:10px; text-decoration:none; color:inherit; animation:fadeUp .4s ease both; }
  @keyframes fadeUp { from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)} }
  .svc::after { content:''; position:absolute; inset:0; background:linear-gradient(135deg,var(--cc,var(--accent)),transparent 60%); opacity:0; transition:opacity .3s; pointer-events:none; }
  .svc:hover { border-color:var(--cc,var(--accent)); transform:translateY(-2px); box-shadow:0 6px 24px rgba(0,0,0,.5),0 0 0 1px var(--cc,var(--accent)); }
  .svc:hover::after { opacity:.04; }
  .svc-top { display:flex; align-items:center; justify-content:space-between; }
  .svc-icon { width:38px; height:38px; display:flex; align-items:center; justify-content:center; background:var(--surface2); border:1px solid var(--border2); font-size:17px; flex-shrink:0; }
  .badge { display:flex; align-items:center; gap:5px; font-size:10px; letter-spacing:.08em; padding:3px 8px; border:1px solid currentColor; }
  .badge.online  { color:var(--online); border-color:rgba(0,255,136,.3);  background:rgba(0,255,136,.05); }
  .badge.exited  { color:var(--offline);border-color:rgba(255,68,85,.3);  background:rgba(255,68,85,.05); }
  .badge.paused  { color:var(--warn);   border-color:rgba(255,170,0,.3);  background:rgba(255,170,0,.05); }
  .bdot { width:5px; height:5px; border-radius:50%; background:currentColor; box-shadow:0 0 5px currentColor; }
  .badge.online .bdot { animation:blink 2s infinite; }
  .svc-name { font-size:14px; font-weight:600; color:var(--text-bright); margin-bottom:2px; }
  .svc-desc { font-size:11px; color:var(--text-dim); line-height:1.5; }
  .svc-foot { display:flex; align-items:center; justify-content:space-between; border-top:1px solid var(--border); padding-top:9px; }
  .svc-tag  { font-size:10px; color:var(--cc,var(--accent)); letter-spacing:.08em; opacity:.75; }
  .svc-meta { display:flex; flex-direction:column; align-items:flex-end; gap:2px; }
  .svc-port { font-size:11px; color:var(--text-dim); }
  .svc-port b { color:var(--text-bright); }
  .svc-perf { font-size:10px; color:var(--text-dim); }
  .svc-uptime { font-size:10px; color:var(--text-dim); max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  /* ── Disco ── */
  .disk-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px; }
  .disk-card { background:var(--surface); border:1px solid var(--border); padding:16px; position:relative; overflow:hidden; }
  .disk-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--dc,var(--accent2)); box-shadow:0 0 6px var(--dc,var(--accent2)); }
  .disk-head { display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:10px; }
  .disk-mount { font-size:14px; font-weight:600; color:var(--text-bright); }
  .disk-device { font-size:10px; color:var(--text-dim); margin-top:2px; }
  .disk-pct { font-size:20px; font-weight:600; color:var(--dc,var(--accent2)); }
  .disk-bar-wrap { background:var(--surface2); height:5px; width:100%; margin-bottom:8px; overflow:hidden; }
  .disk-bar { height:100%; transition:width .7s ease; }
  .disk-stats { display:flex; justify-content:space-between; font-size:11px; color:var(--text-dim); }
  .disk-stats b { color:var(--text-bright); }

  /* ── Hardware ── */
  .hw-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:10px; margin-bottom:10px; }
  .hw-card { background:var(--surface); border:1px solid var(--border); padding:16px; position:relative; overflow:hidden; }
  .hw-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--hw-c,var(--accent)); box-shadow:0 0 8px var(--hw-c,var(--accent)); }
  .hw-label { font-size:10px; color:var(--text-dim); letter-spacing:.12em; text-transform:uppercase; margin-bottom:6px; }
  .hw-val { font-size:26px; font-weight:600; color:var(--hw-c,var(--text-bright)); line-height:1; }
  .hw-unit { font-size:13px; font-weight:400; }
  .hw-sub { font-size:10px; color:var(--text-dim); margin-top:4px; }

  .cost-strip { background:var(--surface); border:1px solid var(--border); padding:14px 20px; display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:16px; position:relative; margin-top:10px; }
  .cost-strip::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,var(--accent4),var(--accent3)); box-shadow:0 0 8px rgba(167,139,250,.4); }
  .cost-item { display:flex; flex-direction:column; gap:3px; }
  .cost-label { font-size:10px; color:var(--text-dim); letter-spacing:.1em; text-transform:uppercase; }
  .cost-val { font-size:15px; font-weight:600; color:var(--text-bright); }
  .cost-val.big { font-size:22px; color:var(--accent4); text-shadow:0 0 12px rgba(167,139,250,.3); }
  .cost-sub { font-size:10px; color:var(--text-dim); }

  /* ── LLM ── */
  .llm-card { background:var(--surface); border:1px solid var(--border); padding:18px; position:relative; overflow:hidden; }
  .llm-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,var(--accent4),var(--accent),var(--accent2)); box-shadow:0 0 14px rgba(167,139,250,.4); }
  .llm-top { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:10px; }
  .llm-title { display:flex; align-items:center; gap:10px; }
  .llm-name { font-size:15px; font-weight:600; color:var(--text-bright); }
  .llm-nbadge { font-size:10px; color:var(--accent4); border:1px solid rgba(167,139,250,.3); background:rgba(167,139,250,.06); padding:2px 8px; letter-spacing:.08em; }
  .llm-body { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; border-top:1px solid var(--border); padding-top:10px; margin-top:10px; }
  .lm-label { font-size:10px; color:var(--text-dim); letter-spacing:.1em; text-transform:uppercase; }
  .lm-value { font-size:13px; color:var(--text-bright); font-weight:500; margin-top:2px; }
  .open-btn { display:inline-flex; align-items:center; gap:4px; font-size:10px; color:var(--accent); text-decoration:none; letter-spacing:.05em; padding:4px 10px; border:1px solid rgba(0,212,255,.3); transition:all .2s; }
  .open-btn:hover { background:rgba(0,212,255,.06); border-color:var(--accent); }

  .footer { margin-top:36px; padding-top:16px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
  .footer-l { font-size:11px; color:var(--text-dim); display:flex; align-items:center; gap:8px; }
  .footer-l::before { content:'//'; color:var(--accent); font-weight:600; }
  .footer-r { font-size:10px; color:var(--text-dim); display:flex; align-items:center; gap:12px; }
  .refresh-btn { font-size:10px; color:var(--accent); background:none; border:1px solid rgba(0,212,255,.3); padding:4px 12px; cursor:pointer; font-family:inherit; letter-spacing:.08em; transition:all .2s; }
  .refresh-btn:hover { background:rgba(0,212,255,.06); }
  .err { color:var(--offline); font-size:11px; padding:12px; border:1px solid rgba(255,68,85,.2); background:rgba(255,68,85,.04); margin-top:8px; }
  @media(max-width:640px){ .services-grid,.disk-grid,.hw-grid{grid-template-columns:1fr} .stats-bar{grid-template-columns:repeat(2,1fr)} }
</style>
</head>
<body>
<div class="grid-bg"></div>
<div class="wrap">

  <header class="header">
    <div>
      <div class="prompt-line"><div class="pdot"></div>arch linux · homelab · docker</div>
      <h1>home<span>lab</span></h1>
      <div class="hd-sub" id="hostname">carregando…</div>
    </div>
    <div>
      <div class="clock" id="clock">--:--:--</div>
      <div class="datestr" id="datestr">---</div>
    </div>
  </header>

  <div class="stats-bar">
    <div class="sc" style="--c:var(--online)"><div class="sc-label">Containers Online</div><div class="sc-val" id="st-online">—</div><div class="sc-sub" id="st-total">de — total</div></div>
    <div class="sc" style="--c:var(--accent)"><div class="sc-label">Imagens Docker</div><div class="sc-val" id="st-images">—</div><div class="sc-sub">em cache local</div></div>
    <div class="sc" style="--c:#38bdf8"><div class="sc-label">CPU Cores</div><div class="sc-val" id="st-cpus">—</div><div class="sc-sub" id="st-arch">—</div></div>
    <div class="sc" style="--c:var(--accent4)"><div class="sc-label">Memória Total</div><div class="sc-val" id="st-mem">—</div><div class="sc-sub">GB RAM</div></div>
  </div>

  <div class="docker-strip" id="docker-strip" style="display:none">
    <div class="di"><div class="di-label">IP Local</div><div class="di-value" id="di-ip">—</div></div>
    <div class="di"><div class="di-label">Docker</div><div class="di-value" id="di-ver">—</div></div>
    <div class="di"><div class="di-label">Kernel</div><div class="di-value" id="di-kernel">—</div></div>
    <div class="di"><div class="di-label">Sistema</div><div class="di-value" id="di-os">—</div></div>
    <div class="di"><div class="di-label">Arq.</div><div class="di-value" id="di-arch">—</div></div>
    <div class="di"><div class="di-label">Host</div><div class="di-value" id="di-host">—</div></div>
  </div>

  <!-- Containers -->
  <div class="section">
    <div class="sec-head"><div class="sec-title">Containers Docker</div><div class="sec-line"></div><div class="sec-count" id="svc-count">carregando…</div></div>
    <div id="svc-grid" class="services-grid"></div>
    <div id="svc-err" class="err" style="display:none"></div>
  </div>

  <!-- LLM -->
  <div class="section">
    <div class="sec-head"><div class="sec-title">Servidor LLM</div><div class="sec-line"></div><div class="sec-count">1 instância nativa</div></div>
    <div class="llm-card">
      <div class="llm-top">
        <div class="llm-title">
          <span style="font-size:21px">🤖</span>
          <div class="llm-name">llama-server</div>
          <div class="llm-nbadge">NATIVO</div>
          <div class="badge" id="llm-badge" style="color:var(--warn);border-color:rgba(255,170,0,.3);background:rgba(255,170,0,.05)">
            <div class="bdot"></div><span>AGUARDANDO</span>
          </div>
        </div>
        <a id="llm-link" href="#" target="_blank" class="open-btn">↗ abrir</a>
      </div>
      <div style="font-size:12px;color:var(--text-dim);line-height:1.6">
        Servidor de inferência local via llama.cpp. Expõe API compatível com OpenAI em
        <span style="color:var(--accent4)">:8080</span>. Roda fora do Docker, direto no host.
      </div>
      <div class="llm-body">
        <div><div class="lm-label">Backend</div><div class="lm-value">llama.cpp</div></div>
        <div><div class="lm-label">Porta</div><div class="lm-value">8080</div></div>
        <div><div class="lm-label">API</div><div class="lm-value">OpenAI-compat.</div></div>
        <div><div class="lm-label">Status</div><div class="lm-value" id="llm-status-text">verificando…</div></div>
      </div>
    </div>
  </div>

  <!-- Disco -->
  <div class="section">
    <div class="sec-head"><div class="sec-title">Armazenamento</div><div class="sec-line"></div><div class="sec-count" id="disk-count">carregando…</div></div>
    <div id="disk-grid" class="disk-grid"></div>
  </div>

  <!-- Hardware & Power -->
  <div class="section">
    <div class="sec-head"><div class="sec-title">Hardware &amp; Energia</div><div class="sec-line"></div><div class="sec-count" id="hw-note">—</div></div>
    <div id="hw-grid" class="hw-grid"></div>
    <div id="cost-strip" class="cost-strip" style="display:none"></div>
    <div id="hw-err" class="err" style="display:none"></div>
  </div>

  <footer class="footer">
    <div class="footer-l">homelab dashboard · arch linux</div>
    <div class="footer-r">
      <span>atualizado: <span id="last-refresh">—</span></span>
      <button class="refresh-btn" onclick="loadAll()">↻ atualizar</button>
    </div>
  </footer>
</div>

<script>
let LOCAL_IP = location.hostname;

// Clock
function tick() {
  const n=new Date(), p=v=>String(v).padStart(2,'0');
  document.getElementById('clock').textContent=`${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`;
  const D=['Dom','Seg','Ter','Qua','Qui','Sex','Sáb'],M=['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
  document.getElementById('datestr').textContent=`${D[n.getDay()]}, ${p(n.getDate())} ${M[n.getMonth()]} ${n.getFullYear()}`;
}
setInterval(tick,1000); tick();

const stateLabel=s=>({running:'ONLINE',exited:'OFFLINE',paused:'PAUSADO',restarting:'RESTART'}[s]||s.toUpperCase());
const stateClass=s=>({running:'online',exited:'exited',paused:'paused'}[s]||'exited');
const diskColor=p=>p>=90?'#ff4455':p>=70?'#ffaa00':'#00ff88';
const tempColor=t=>t>=85?'#ff4455':t>=70?'#ffaa00':'#00d4ff';

async function loadLocalIp() {
  try {
    const d = await (await fetch('/api/local-ip')).json();
    LOCAL_IP = d.ip;
    document.getElementById('llm-link').href = `http://${LOCAL_IP}:8080`;
    document.getElementById('di-ip').textContent = LOCAL_IP;
  } catch(e) {}
}

async function loadDockerInfo() {
  try {
    const d = await (await fetch('/api/docker/info')).json();
    document.getElementById('st-images').textContent = d.images;
    document.getElementById('st-cpus').textContent   = d.cpus;
    document.getElementById('st-arch').textContent   = d.arch;
    document.getElementById('st-mem').textContent    = d.mem_total_gb;
    document.getElementById('hostname').textContent  = `${d.hostname} · ${d.os}`;
    document.getElementById('di-ver').textContent    = d.docker_version;
    document.getElementById('di-kernel').textContent = d.kernel;
    document.getElementById('di-os').textContent     = d.os;
    document.getElementById('di-arch').textContent   = d.arch;
    document.getElementById('di-host').textContent   = d.hostname;
    document.getElementById('docker-strip').style.display='flex';
  } catch(e) {}
}

async function loadContainers() {
  const err=document.getElementById('svc-err');
  try {
    const data = await (await fetch('/api/containers')).json();
    err.style.display='none';
    document.getElementById('svc-count').textContent=`${data.length} containers`;
    document.getElementById('st-online').textContent=data.filter(s=>s.state==='running').length;
    document.getElementById('st-total').textContent=`de ${data.length} total`;
    document.getElementById('svc-grid').innerHTML=data.map((s,i)=>{
      const href=s.port?`http://${LOCAL_IP}:${s.port}`:'#';
      const perf=s.state==='running'?`CPU ${s.cpu_percent}% · MEM ${s.mem_mb} MB`:'';
      return `<a class="svc" href="${href}" target="${s.port?'_blank':'_self'}" style="--cc:${s.color};animation-delay:${0.05+i*.04}s"${!s.port?' onclick="return false"':''}>
        <div class="svc-top">
          <div class="svc-icon">${s.icon}</div>
          <div class="badge ${stateClass(s.state)}"><div class="bdot"></div><span>${stateLabel(s.state)}</span></div>
        </div>
        <div><div class="svc-name">${s.name}</div><div class="svc-desc">${s.desc}</div></div>
        <div class="svc-foot">
          <div class="svc-tag">${s.tag}</div>
          <div class="svc-meta">
            ${s.port?`<div class="svc-port">:<b>${s.port}</b></div>`:''}
            ${perf?`<div class="svc-perf">${perf}</div>`:''}
            <div class="svc-uptime" title="${s.status}">${s.status}</div>
          </div>
        </div>
      </a>`;
    }).join('');
  } catch(e) {
    err.style.display='block';
    err.textContent=`Erro ao consultar Docker: ${e.message}`;
  }
}

async function loadLlama() {
  try {
    const d=await (await fetch('/api/llama')).json();
    const b=document.getElementById('llm-badge'), t=document.getElementById('llm-status-text');
    if (d.running) {
      b.className='badge online'; b.innerHTML='<div class="bdot"></div><span>ONLINE</span>';
      t.textContent=d.status||'ok'; t.style.color='var(--online)';
    } else {
      b.className='badge exited'; b.innerHTML='<div class="bdot"></div><span>OFFLINE</span>';
      t.textContent='inacessível'; t.style.color='var(--offline)';
    }
  } catch(e) {}
}

async function loadDisk() {
  try {
    const data=await (await fetch('/api/disk')).json();
    document.getElementById('disk-count').textContent=`${data.length} partições`;
    document.getElementById('disk-grid').innerHTML=data.map(d=>{
      if (d.error) return `<div class="disk-card"><div class="disk-mount">Erro</div><div class="disk-device">${d.error}</div></div>`;
      const col=diskColor(d.percent);
      return `<div class="disk-card" style="--dc:${col}">
        <div class="disk-head">
          <div><div class="disk-mount">${d.mountpoint}</div><div class="disk-device">${d.device} · ${d.fstype}</div></div>
          <div class="disk-pct">${d.percent}%</div>
        </div>
        <div class="disk-bar-wrap"><div class="disk-bar" style="width:${d.percent}%;background:${col};box-shadow:0 0 6px ${col}80"></div></div>
        <div class="disk-stats"><span>Usado <b>${d.used_gb} GB</b></span><span>Livre <b>${d.free_gb} GB</b></span><span>Total <b>${d.total_gb} GB</b></span></div>
      </div>`;
    }).join('');
  } catch(e) {}
}

async function loadHardware() {
  const err=document.getElementById('hw-err');
  try {
    const d=await (await fetch('/api/hardware')).json();
    const cards=[];

    if (d.cpu?.temp_c!=null) {
      const col=tempColor(d.cpu.temp_c);
      cards.push(`<div class="hw-card" style="--hw-c:${col}"><div class="hw-label">Temp CPU</div><div class="hw-val">${d.cpu.temp_c}<span class="hw-unit"> °C</span></div><div class="hw-sub">média dos cores</div></div>`);
    }
    if (d.cpu?.usage_pct!=null) {
      const col=d.cpu.usage_pct>80?'#ff4455':d.cpu.usage_pct>50?'#ffaa00':'#00d4ff';
      cards.push(`<div class="hw-card" style="--hw-c:${col}"><div class="hw-label">Uso CPU</div><div class="hw-val">${d.cpu.usage_pct}<span class="hw-unit"> %</span></div><div class="hw-sub">todos os cores</div></div>`);
    }
    if (d.cpu?.power_w!=null) {
      cards.push(`<div class="hw-card" style="--hw-c:#38bdf8"><div class="hw-label">Potência CPU</div><div class="hw-val">${d.cpu.power_w}<span class="hw-unit"> W</span></div><div class="hw-sub">Intel RAPL</div></div>`);
    }
    if (d.gpu?.temp_c!=null) {
      const col=tempColor(d.gpu.temp_c);
      cards.push(`<div class="hw-card" style="--hw-c:${col}"><div class="hw-label">Temp GPU</div><div class="hw-val">${d.gpu.temp_c}<span class="hw-unit"> °C</span></div><div class="hw-sub">${d.gpu.name||'GPU'}</div></div>`);
    }
    if (d.gpu?.power_w!=null) {
      cards.push(`<div class="hw-card" style="--hw-c:#f97316"><div class="hw-label">Potência GPU</div><div class="hw-val">${d.gpu.power_w}<span class="hw-unit"> W</span></div><div class="hw-sub">${d.gpu.power_limit_w?`limite ${d.gpu.power_limit_w} W`:d.gpu.name||'GPU'}</div></div>`);
    }
    if (d.gpu?.util_pct!=null) {
      const col=d.gpu.util_pct>80?'#ff4455':d.gpu.util_pct>50?'#ffaa00':'#a78bfa';
      const vram=d.gpu.vram_used_mb?`VRAM ${Math.round(d.gpu.vram_used_mb/102.4)/10}/${Math.round(d.gpu.vram_total_mb/102.4)/10} GB`:'';
      cards.push(`<div class="hw-card" style="--hw-c:${col}"><div class="hw-label">Uso GPU</div><div class="hw-val">${d.gpu.util_pct}<span class="hw-unit"> %</span></div><div class="hw-sub">${vram}</div></div>`);
    }
    if (d.total_w!=null) {
      cards.push(`<div class="hw-card" style="--hw-c:#fb923c"><div class="hw-label">Consumo Total</div><div class="hw-val">${d.total_w}<span class="hw-unit"> W</span></div><div class="hw-sub">CPU + GPU + overhead</div></div>`);
    }

    if (!cards.length) {
      cards.push(`<div class="hw-card" style="--hw-c:var(--text-dim)"><div class="hw-label">Hardware</div><div class="hw-val" style="font-size:14px;color:var(--text-dim)">N/D</div><div class="hw-sub">instale lm-sensors · psutil</div></div>`);
      document.getElementById('hw-note').textContent='dados indisponíveis';
    } else {
      document.getElementById('hw-note').textContent=`${cards.length} sensores`;
    }
    document.getElementById('hw-grid').innerHTML=cards.join('');

    const cost=d.cost, strip=document.getElementById('cost-strip');
    if (cost && Object.keys(cost).length) {
      strip.style.display='grid';
      strip.innerHTML=`
        <div class="cost-item"><div class="cost-label">Por Segundo</div><div class="cost-val">€ ${cost.per_second_eur.toFixed(6)}</div><div class="cost-sub">${(cost.per_second_eur*3600).toFixed(4)} €/h</div></div>
        <div class="cost-item"><div class="cost-label">Por Hora</div><div class="cost-val">€ ${cost.per_hour_eur.toFixed(4)}</div><div class="cost-sub">taxa ${cost.eur_kwh_rate} €/kWh</div></div>
        <div class="cost-item"><div class="cost-label">Por Dia</div><div class="cost-val">€ ${cost.per_day_eur.toFixed(3)}</div><div class="cost-sub">24h contínuo</div></div>
        <div class="cost-item"><div class="cost-label">Estimativa Mensal</div><div class="cost-val big">€ ${cost.per_month_eur.toFixed(2)}</div><div class="cost-sub">30 dias · ${d.total_w} W médios</div></div>`;
    } else {
      strip.style.display='none';
    }
    err.style.display='none';
  } catch(e) {
    err.style.display='block';
    err.textContent=`Erro hardware: ${e.message}`;
  }
}

async function loadAll() {
  await loadLocalIp();
  await Promise.all([loadDockerInfo(), loadContainers(), loadLlama(), loadDisk(), loadHardware()]);
  const n=new Date(), p=v=>String(v).padStart(2,'0');
  document.getElementById('last-refresh').textContent=`${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`;
}

loadAll();
setInterval(loadAll, 15000);
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
