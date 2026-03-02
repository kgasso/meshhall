#!/usr/bin/env bash
# =============================================================================
# MeshHall Uninstall Script
# =============================================================================
# Usage:
#   sudo bash uninstall.sh                  # remove everything except database
#   sudo bash uninstall.sh --delete-db      # remove everything including database
#   sudo bash uninstall.sh --yes            # skip confirmation prompts
#   sudo bash uninstall.sh --install-dir /srv/meshhall  # custom install path
#
# What this removes:
#   - systemd services (stopped, disabled, unit files deleted)
#   - /opt/meshhall/  (code, venv, config, logs — but NOT data/ by default)
#   - meshhall system user and group
#
# What this keeps by default:
#   - /opt/meshhall/data/  (database and log file)
#
# The database contains your user registry, bulletin board, check-in history,
# frequency directory, and weather alert cache. Pass --delete-db only when you
# are certain you will not need it again.
# =============================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/meshhall"
SERVICE_USER="meshhall"
DELETE_DB=false
ASSUME_YES=false

# ── Colour helpers ─────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

info()   { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()     { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()   { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()  { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
header() { echo -e "\n${BOLD}=== $* ===${RESET}"; }
die()    { error "$*"; exit 1; }

confirm() {
    local prompt="$1"
    local default="${2:-default_yes}"
    if $ASSUME_YES; then return 0; fi
    if [ "$default" = "default_yes" ]; then
        read -rp "$(echo -e "${YELLOW}?${RESET} $prompt [Y/n] ")" reply
        [[ "${reply:-y}" =~ ^[Yy]$ ]]
    else
        read -rp "$(echo -e "${YELLOW}?${RESET} $prompt [y/N] ")" reply
        [[ "${reply:-n}" =~ ^[Yy]$ ]]
    fi
}

# ── Argument parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --delete-db)         DELETE_DB=true ;;
        --yes|-y)            ASSUME_YES=true ;;
        --install-dir)       INSTALL_DIR="$2"; shift ;;
        --install-dir=*)     INSTALL_DIR="${1#*=}" ;;
        --help|-h)
            echo "Usage: sudo bash uninstall.sh [--delete-db] [--yes] [--install-dir PATH]"
            echo ""
            echo "  --delete-db      Also delete the database and log (data/ directory)."
            echo "                   DEFAULT: data/ is kept so you don't lose your data."
            echo "  --yes            Skip all confirmation prompts."
            echo "  --install-dir    Path where MeshHall is installed (default: /opt/meshhall)."
            exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
    shift
done

# ── Pre-flight ─────────────────────────────────────────────────────────────────
header "MeshHall Uninstaller"

[ "$EUID" -eq 0 ] || die "This script must be run as root (use sudo)."

if [ ! -d "$INSTALL_DIR" ] && ! id "$SERVICE_USER" &>/dev/null; then
    die "MeshHall does not appear to be installed (no directory at $INSTALL_DIR and no '$SERVICE_USER' user)."
fi

# ── Summarise what will be removed ────────────────────────────────────────────
echo ""
info "This will remove:"
echo "    - systemd services: meshhall, meshhall-same"
echo "    - /etc/systemd/system/meshhall.service"
echo "    - /etc/systemd/system/meshhall-same.service"
echo "    - $INSTALL_DIR/{code, config, venv, ...}  (all except data/)"
echo "    - system user/group: $SERVICE_USER"

if $DELETE_DB; then
    echo ""
    warn "  *** --delete-db is set: $INSTALL_DIR/data/ will also be deleted ***"
    warn "  *** This includes meshhall.db (user registry, bulletins, check-ins, ***"
    warn "  *** frequency directory, weather alerts) and meshhall.log.          ***"
    echo ""
else
    echo ""
    info "  Database preserved: $INSTALL_DIR/data/ will NOT be deleted."
    info "  Remove manually if no longer needed: sudo rm -rf $INSTALL_DIR/data/"
fi

echo ""
confirm "Proceed with uninstall?" "default_no" || { info "Uninstall cancelled."; exit 0; }

# ── Stop and disable services ─────────────────────────────────────────────────
header "Stopping Services"

for svc in meshhall meshhall-same; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Stopping ${svc}..."
        systemctl stop "$svc"
        ok "${svc} stopped."
    else
        info "${svc} is not running."
    fi

    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        info "Disabling ${svc}..."
        systemctl disable "$svc"
        ok "${svc} disabled."
    fi
done

# ── Remove systemd unit files ─────────────────────────────────────────────────
header "Removing Systemd Unit Files"

for unit in meshhall.service meshhall-same.service; do
    path="/etc/systemd/system/$unit"
    if [ -f "$path" ]; then
        rm -f "$path"
        ok "Removed $path"
    else
        info "$path not found — skipping."
    fi
done

systemctl daemon-reload
ok "systemd daemon reloaded."

# ── Remove install directory ───────────────────────────────────────────────────
header "Removing Installation Files"

if [ -d "$INSTALL_DIR" ]; then
    if $DELETE_DB; then
        # Remove everything
        info "Removing $INSTALL_DIR (including data/)..."
        rm -rf "$INSTALL_DIR"
        ok "$INSTALL_DIR removed."
    else
        # Remove everything except data/
        info "Removing $INSTALL_DIR (preserving data/)..."

        # Backup data/ temporarily, nuke the directory, restore data/
        TMP_DATA="$(mktemp -d)"
        if [ -d "$INSTALL_DIR/data" ]; then
            cp -a "$INSTALL_DIR/data/." "$TMP_DATA/"
            DATA_SAVED=true
        else
            DATA_SAVED=false
        fi

        rm -rf "$INSTALL_DIR"

        if $DATA_SAVED; then
            mkdir -p "$INSTALL_DIR/data"
            cp -a "$TMP_DATA/." "$INSTALL_DIR/data/"
            rm -rf "$TMP_DATA"

            # Restore ownership if the user still exists (it does — we remove them next)
            if id "$SERVICE_USER" &>/dev/null; then
                chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" 2>/dev/null || true
            fi
            ok "Code removed. Data preserved at $INSTALL_DIR/data/"
            info "Files in $INSTALL_DIR/data/:"
            ls -lh "$INSTALL_DIR/data/" 2>/dev/null || true
        else
            ok "$INSTALL_DIR removed (no data/ directory was present)."
        fi
    fi
else
    info "$INSTALL_DIR not found — skipping directory removal."
fi

# ── Remove service user ────────────────────────────────────────────────────────
header "Removing Service Account"

if id "$SERVICE_USER" &>/dev/null; then
    info "Removing user '${SERVICE_USER}'..."
    # --remove would delete home dir; we manage that ourselves above
    userdel "$SERVICE_USER"
    ok "User '${SERVICE_USER}' removed."
else
    info "User '${SERVICE_USER}' not found — skipping."
fi

# Remove the group if it still exists (userdel may or may not remove it)
if getent group "$SERVICE_USER" &>/dev/null; then
    groupdel "$SERVICE_USER" 2>/dev/null && ok "Group '${SERVICE_USER}' removed." || true
fi

# ── Summary ────────────────────────────────────────────────────────────────────
header "Done"

if $DELETE_DB; then
    ok "MeshHall fully uninstalled. All data removed."
else
    ok "MeshHall uninstalled."
    echo ""
    info "Your database and logs are preserved at:"
    echo "    $INSTALL_DIR/data/"
    echo ""
    info "To remove them when you're sure you no longer need them:"
    echo "    sudo rm -rf $INSTALL_DIR"
fi
echo ""
