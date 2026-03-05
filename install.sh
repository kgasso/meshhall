#!/usr/bin/env bash
# =============================================================================
# MeshHall Install / Upgrade Script
# =============================================================================
# Usage:
#   Fresh install:   sudo bash install.sh
#   Upgrade:         sudo bash install.sh
#   Skip prompts:    sudo bash install.sh --yes
#   Custom path:     sudo bash install.sh --install-dir /srv/meshhall
#
# The script detects whether MeshHall is already installed and behaves
# accordingly — preserving all config on upgrade, warning about diffs.
# =============================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/meshhall"
SERVICE_USER="meshhall"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSUME_YES=false
FRESH_INSTALL=false

# Colour helpers (disabled if not a terminal)
if [ -t 1 ]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
header()  { echo -e "\n${BOLD}=== $* ===${RESET}"; }
die()     { error "$*"; exit 1; }

confirm() {
    # confirm "Question?" default_yes|default_no
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

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)        ASSUME_YES=true ;;
        --install-dir)   INSTALL_DIR="$2"; shift ;;
        --install-dir=*) INSTALL_DIR="${1#*=}" ;;
        --help|-h)
            echo "Usage: sudo bash install.sh [--yes] [--install-dir PATH]"
            exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
    shift
done

# ── Pre-flight checks ─────────────────────────────────────────────────────────
header "MeshHall Installer"

[ "$EUID" -eq 0 ] || die "This script must be run as root (use sudo)."

# Check we're running from the source directory
[ -f "$SCRIPT_DIR/meshhall.py" ] || die "Run this script from the MeshHall source directory."
[ -f "$SCRIPT_DIR/requirements.txt" ] || die "requirements.txt not found in source directory."

# Detect fresh install vs upgrade
if [ -f "$INSTALL_DIR/meshhall.py" ]; then
    FRESH_INSTALL=false
    info "Existing installation detected at ${INSTALL_DIR} — running in upgrade mode."
else
    FRESH_INSTALL=true
    info "No existing installation found — running fresh install."
fi

# ── System dependencies ───────────────────────────────────────────────────────
header "System Dependencies"

MISSING_PKGS=()
for pkg in python3 python3-venv python3-pip; do
    dpkg -s "$pkg" &>/dev/null || MISSING_PKGS+=("$pkg")
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    info "Installing missing packages: ${MISSING_PKGS[*]}"
    apt-get update -qq
    apt-get install -y -qq "${MISSING_PKGS[@]}"
else
    ok "Python dependencies already installed."
fi

# ── Service account ───────────────────────────────────────────────────────────
header "Service Account"

if id "$SERVICE_USER" &>/dev/null; then
    ok "User '${SERVICE_USER}' already exists."
else
    info "Creating system user '${SERVICE_USER}'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "User '${SERVICE_USER}' created."
fi

# Ensure group memberships
for grp in dialout; do
    if getent group "$grp" &>/dev/null; then
        if id -nG "$SERVICE_USER" | grep -qw "$grp"; then
            ok "User '${SERVICE_USER}' already in group '${grp}'."
        else
            usermod -aG "$grp" "$SERVICE_USER"
            ok "Added '${SERVICE_USER}' to group '${grp}'."
        fi
    else
        warn "Group '${grp}' not found — skipping."
    fi
done

# ── Stop services if upgrading ────────────────────────────────────────────────
if ! $FRESH_INSTALL; then
    header "Stopping Services"
    if systemctl is-active --quiet meshhall 2>/dev/null; then
        info "Stopping meshhall..."
        systemctl stop meshhall
        ok "meshhall stopped."
    fi
fi

# ── Config handling ───────────────────────────────────────────────────────────
header "Configuration"

# Build list of all config files from source
mapfile -t SRC_CONFIGS < <(find "$SCRIPT_DIR/config" -type f -name "*.yaml" | sort)

CONFIG_DIFFS=()    # files that exist in both and differ
CONFIG_NEW=()      # files new in this release, not present in install

if $FRESH_INSTALL; then
    info "Fresh install — copying all config files."
    mkdir -p "$INSTALL_DIR/config/plugins"
    cp -r "$SCRIPT_DIR/config/." "$INSTALL_DIR/config/"
else
    info "Upgrade — checking config files for differences..."
    mkdir -p "$INSTALL_DIR/config/plugins"

    for src_file in "${SRC_CONFIGS[@]}"; do
        # Compute relative path (e.g. config/plugins/weather.yaml)
        rel="${src_file#"$SCRIPT_DIR/"}"
        dst_file="$INSTALL_DIR/$rel"

        if [ ! -f "$dst_file" ]; then
            # New config file introduced in this release
            CONFIG_NEW+=("$rel")
        elif ! diff -q "$src_file" "$dst_file" &>/dev/null; then
            # File exists but differs from source default
            CONFIG_DIFFS+=("$rel")
        fi
    done

    # Copy new config files automatically (safe — they don't exist yet)
    if [ ${#CONFIG_NEW[@]} -gt 0 ]; then
        echo ""
        info "New config files in this release (copying automatically):"
        for f in "${CONFIG_NEW[@]}"; do
            echo "    + $f"
            mkdir -p "$INSTALL_DIR/$(dirname "$f")"
            cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
            chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/$f"
        done
        ok "${#CONFIG_NEW[@]} new config file(s) copied."
    fi

    # Warn about diffs — show them and let the user decide
    if [ ${#CONFIG_DIFFS[@]} -gt 0 ]; then
        echo ""
        warn "The following config files differ from the new release defaults:"
        for f in "${CONFIG_DIFFS[@]}"; do
            echo "    ~ $f"
        done
        echo ""
        warn "Your installed versions have been preserved."
        warn "Review the differences below to see if you need to add new settings:"
        echo ""

        for f in "${CONFIG_DIFFS[@]}"; do
            echo -e "${BOLD}--- Diff: $f ---${RESET}"
            # Show a clean diff: - = your version, + = new default
            diff --color=never \
                 --label "installed: $f" \
                 --label "new default: $f" \
                 "$INSTALL_DIR/$f" "$SCRIPT_DIR/$f" || true
            echo ""
        done

        # Offer to save new defaults alongside installed versions for reference
        if confirm "Save new default configs as .new files for manual comparison?" "default_yes"; then
            for f in "${CONFIG_DIFFS[@]}"; do
                cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/${f}.new"
                chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/${f}.new"
                info "Saved: ${INSTALL_DIR}/${f}.new"
            done
            echo ""
            info "Compare and merge manually, then delete the .new files when done."
            info "Example: diff /opt/meshhall/config/plugins/weather.yaml{,.new}"
        fi
    else
        ok "All config files match release defaults — no action needed."
    fi
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config"

# ── Data files ────────────────────────────────────────────────────────────────
header "Data Files"

# Enumerated data files shipped with the release.
# On fresh install: always copy. On upgrade: warn and prompt if file differs,
# same as config handling — an operator may have substituted their own dataset.
#
# NOTE: data files live in the source root (one level up from deploy/),
# so we reference them via SOURCE_ROOT rather than SCRIPT_DIR.
SOURCE_ROOT="$SCRIPT_DIR"  # install.sh lives at source root

DATA_FILES=(
    "data/zip_code_database.csv"
)

DATA_DIFFS=()   # data files that exist in both and differ
DATA_NEW=()     # data files not yet present in install dir

mkdir -p "$INSTALL_DIR/data"

for rel in "${DATA_FILES[@]}"; do
    src_file="$SOURCE_ROOT/$rel"
    dst_file="$INSTALL_DIR/$rel"

    if [ ! -f "$src_file" ]; then
        warn "Source data file missing: $rel — skipping."
        continue
    fi

    if $FRESH_INSTALL || [ ! -f "$dst_file" ]; then
        DATA_NEW+=("$rel")
    elif ! diff -q "$src_file" "$dst_file" &>/dev/null; then
        DATA_DIFFS+=("$rel")
    else
        ok "$rel — unchanged."
    fi
done

# Copy new/missing data files automatically
if [ ${#DATA_NEW[@]} -gt 0 ]; then
    for rel in "${DATA_NEW[@]}"; do
        info "Installing $rel..."
        cp "$SOURCE_ROOT/$rel" "$INSTALL_DIR/$rel"
        chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/$rel"
        ok "$rel installed."
    done
fi

# Warn about changed data files — prompt before overwriting
if [ ${#DATA_DIFFS[@]} -gt 0 ]; then
    echo ""
    warn "The following data files differ from the release version:"
    for rel in "${DATA_DIFFS[@]}"; do
        src_size=$(wc -l < "$SOURCE_ROOT/$rel")
        dst_size=$(wc -l < "$INSTALL_DIR/$rel")
        echo "    ~ $rel  (installed: ${dst_size} lines, release: ${src_size} lines)"
    done
    echo ""
    warn "You may have a customised dataset installed. The release version will"
    warn "not be copied unless you confirm below."
    echo ""

    for rel in "${DATA_DIFFS[@]}"; do
        if confirm "Replace installed $rel with release version?" "default_no"; then
            # Save a backup of the existing file before overwriting
            bak="$INSTALL_DIR/${rel}.bak"
            cp "$INSTALL_DIR/$rel" "$bak"
            chown "$SERVICE_USER:$SERVICE_USER" "$bak"
            info "Backup saved: $bak"
            cp "$SOURCE_ROOT/$rel" "$INSTALL_DIR/$rel"
            chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/$rel"
            ok "$rel updated."
        else
            info "$rel unchanged — keeping installed version."
        fi
    done
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data"

# ── Deploy code ───────────────────────────────────────────────────────────────
header "Deploying Code"

# Create directory structure
mkdir -p "$INSTALL_DIR"/{core,plugins,tools,deploy,data}

# Code directories — always overwrite
for dir in core plugins tools; do
    info "Deploying ${dir}/..."
    cp -r "$SCRIPT_DIR/$dir/." "$INSTALL_DIR/$dir/"
done

# Top-level files
for f in meshhall.py requirements.txt; do
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
done

# Deploy service files (always update — they reference paths, not user config)
cp "$SCRIPT_DIR/deploy/meshhall.service" /etc/systemd/system/meshhall.service

# Fix ownership
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

ok "Code deployed."

# ── Virtual environment ───────────────────────────────────────────────────────
header "Python Virtual Environment"

VENV="$INSTALL_DIR/venv"

if [ ! -d "$VENV" ]; then
    info "Creating virtual environment..."
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV"
    ok "Virtual environment created."
else
    ok "Virtual environment already exists."
fi

info "Installing/updating Python dependencies..."
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --quiet --no-cache-dir --upgrade pip
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --quiet --no-cache-dir -r "$INSTALL_DIR/requirements.txt"
ok "Python dependencies up to date."

# ── Systemd ───────────────────────────────────────────────────────────────────
header "Systemd Services"

systemctl daemon-reload

if systemctl is-enabled --quiet meshhall 2>/dev/null; then
    ok "meshhall already enabled."
else
    if confirm "Enable meshhall service to start on boot?"; then
        systemctl enable meshhall
        ok "meshhall enabled."
    else
        warn "meshhall not enabled — start manually with: sudo systemctl start meshhall"
    fi
fi

# ── First-run config reminder ─────────────────────────────────────────────────
if $FRESH_INSTALL; then
    header "First-Run Configuration Required"
    echo ""
    warn "Before starting MeshHall, edit these files:"
    echo ""
    echo "  Main config:"
    echo "    sudo -u meshhall nano $INSTALL_DIR/config/config.yaml"
    echo "    → Set bot.admins (your node ID)"
    echo "    → Set connection.serial_port (verify with: ls /dev/ttyACM*)"
    echo ""
    echo "  Weather plugin:"
    echo "    sudo -u meshhall nano $INSTALL_DIR/config/plugins/weather.yaml"
    echo "    → Set zone (find at weather.gov/pimar/PubZone)"
    echo "    → Set lat/lon"
    echo ""
    echo "  Time plugin:"
    echo "    sudo -u meshhall nano $INSTALL_DIR/config/plugins/time.yaml"
    echo "    → Set timezone"
    echo ""
    echo "  Frequency directory:"
    echo "    sudo -u meshhall nano $INSTALL_DIR/config/plugins/frequencies.yaml"
    echo "    → Add local repeaters and emergency frequencies"
    echo ""
  fi

# ── Start services ────────────────────────────────────────────────────────────
header "Starting Services"

if $FRESH_INSTALL; then
    if confirm "Start MeshHall now? (Recommended to configure first — see above)." "default_no"; then
        systemctl start meshhall
        ok "meshhall started."
    else
        info "Run 'sudo systemctl start meshhall' when ready."
    fi
else
    if confirm "Start MeshHall now?"; then
        systemctl start meshhall
        sleep 2
        if systemctl is-active --quiet meshhall; then
            ok "meshhall started successfully."
        else
            error "meshhall failed to start. Check logs:"
            journalctl -u meshhall -n 30 --no-pager
            exit 1
        fi
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
header "Done"

if $FRESH_INSTALL; then
    ok "MeshHall installed to ${INSTALL_DIR}"
else
    ok "MeshHall upgraded at ${INSTALL_DIR}"
fi

echo ""
echo "  Useful commands:"
echo "    sudo systemctl status meshhall"
echo "    journalctl -u meshhall -f"
echo "    sudo systemctl restart meshhall"
echo "    sudo systemctl stop meshhall"
echo """"
if [ ${#CONFIG_DIFFS[@]} -gt 0 ] 2>/dev/null; then
    warn "Remember to review config diffs noted above."
    warn "New default files saved with .new extension in $INSTALL_DIR/config/"
fi
