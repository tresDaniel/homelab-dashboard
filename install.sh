#!/usr/bin/env bash
# =============================================================================
# Homelab Dashboard — Script de instalação (Arch Linux)
# =============================================================================
set -e

INSTALL_DIR="/opt/homelab-dashboard"
SERVICE_FILE="/etc/systemd/system/homelab-dashboard.service"
CURRENT_USER="${SUDO_USER:-$USER}"

echo ""
echo "  ██╗  ██╗ ██████╗ ███╗   ███╗███████╗██╗      █████╗ ██████╗ "
echo "  ██║  ██║██╔═══██╗████╗ ████║██╔════╝██║     ██╔══██╗██╔══██╗"
echo "  ███████║██║   ██║██╔████╔██║█████╗  ██║     ███████║██████╔╝"
echo "  ██╔══██║██║   ██║██║╚██╔╝██║██╔══╝  ██║     ██╔══██║██╔══██╗"
echo "  ██║  ██║╚██████╔╝██║ ╚═╝ ██║███████╗███████╗██║  ██║██████╔╝"
echo "  ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═════╝ "
echo ""
echo "  Dashboard · Arch Linux · Docker Socket"
echo "  ----------------------------------------"
echo ""

# Verificações
if [[ $EUID -ne 0 ]]; then
  echo "  [!] Execute como root: sudo ./install.sh"
  exit 1
fi

echo "  [1/5] Verificando dependências..."
command -v python3 >/dev/null || { echo "  [!] python3 não encontrado. Instale: sudo pacman -S python"; exit 1; }
command -v docker  >/dev/null || { echo "  [!] docker não encontrado. Instale: sudo pacman -S docker"; exit 1; }

# Verifica acesso ao socket
if [[ ! -S /var/run/docker.sock ]]; then
  echo "  [!] Docker socket não encontrado. Inicie o docker: sudo systemctl start docker"
  exit 1
fi

echo "  [2/5] Copiando arquivos para $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp main.py requirements.txt "$INSTALL_DIR/"

echo "  [3/5] Criando virtualenv e instalando dependências..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo "  [4/5] Instalando serviço systemd..."
sed "s/YOUR_USER/$CURRENT_USER/g" homelab-dashboard.service > "$SERVICE_FILE"

# Garante que o usuário está no grupo docker
if ! id -nG "$CURRENT_USER" | grep -qw docker; then
  echo "  [~] Adicionando $CURRENT_USER ao grupo docker..."
  usermod -aG docker "$CURRENT_USER"
  echo "  [~] AVISO: Faça logout e login para o grupo docker ter efeito."
fi

echo "  [5/5] Habilitando e iniciando serviço..."
systemctl daemon-reload
systemctl enable homelab-dashboard
systemctl restart homelab-dashboard

echo ""
echo "  ✓ Instalação concluída!"
echo ""
echo "  Dashboard rodando em: http://localhost:8000"
echo "  Status do serviço:    sudo systemctl status homelab-dashboard"
echo "  Logs:                 sudo journalctl -u homelab-dashboard -f"
echo ""
