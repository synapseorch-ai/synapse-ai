#!/bin/bash

set -e

# Detect if script is being sourced (. setup.sh) vs executed (bash setup.sh)
_SOURCED=false
[[ "${BASH_SOURCE[0]}" != "${0}" ]] && _SOURCED=true

# ---------------------------------------------------------------------------
# Detect OS and distribution
# ---------------------------------------------------------------------------
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        OS="linux"
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            DISTRO=$ID
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
    elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        OS="windows"
    else
        OS="unknown"
    fi
}

# ---------------------------------------------------------------------------
# Install Git if missing
# ---------------------------------------------------------------------------
install_git() {
    echo ""
    echo "Installing Git..."
    
    if [[ "$OS" == "linux" ]]; then
        if [[ "$DISTRO" == "ubuntu" ]] || [[ "$DISTRO" == "debian" ]]; then
            echo "Installing Git on Ubuntu/Debian..."
            sudo apt-get update
            sudo apt-get install -y git
        elif [[ "$DISTRO" == "fedora" ]] || [[ "$DISTRO" == "rhel" ]] || [[ "$DISTRO" == "centos" ]]; then
            echo "Installing Git on Fedora/RHEL..."
            sudo dnf install -y git
        elif [[ "$DISTRO" == "arch" ]] || [[ "$DISTRO" == "manjaro" ]]; then
            echo "Installing Git on Arch/Manjaro..."
            sudo pacman -S --noconfirm git
        else
            echo "Unknown Linux distribution: $DISTRO"
            exit 1
        fi
    elif [[ "$OS" == "macos" ]]; then
        echo "Installing Git via Homebrew..."
        if ! command -v brew &> /dev/null; then
            echo "Homebrew not found. Please install from https://brew.sh"
            exit 1
        fi
        brew install git
    elif [[ "$OS" == "windows" ]]; then
        echo "Please download and install Git from https://git-scm.com/download/win"
        exit 1
    fi
    
    echo "✓ Git installed successfully"
}

# ---------------------------------------------------------------------------
# Install Python if missing
# ---------------------------------------------------------------------------
install_python() {
    echo ""
    echo "Installing Python 3.11+..."
    
    if [[ "$OS" == "linux" ]]; then
        if [[ "$DISTRO" == "ubuntu" ]] || [[ "$DISTRO" == "debian" ]]; then
            echo "Installing Python 3.11 on Ubuntu/Debian..."
            sudo apt-get update -qq
            # python3.11 is in the default repos on Ubuntu 22.04+; try that first
            if ! sudo apt-get install -y python3.11 python3.11-venv python3-pip 2>/dev/null; then
                # Older distros (Ubuntu 20.04) need the deadsnakes PPA
                echo "python3.11 not in default repos — adding deadsnakes PPA..."
                sudo apt-get install -y software-properties-common
                sudo add-apt-repository -y ppa:deadsnakes/ppa
                sudo apt-get update -qq
                sudo apt-get install -y python3.11 python3.11-venv python3-pip
            fi
        elif [[ "$DISTRO" == "fedora" ]] || [[ "$DISTRO" == "rhel" ]] || [[ "$DISTRO" == "centos" ]]; then
            echo "Installing Python on Fedora/RHEL..."
            sudo dnf install -y python3.11 python3-pip 2>/dev/null || \
                sudo dnf install -y python3 python3-pip
        elif [[ "$DISTRO" == "arch" ]] || [[ "$DISTRO" == "manjaro" ]]; then
            echo "Installing Python on Arch/Manjaro..."
            sudo pacman -S --noconfirm python python-pip
        else
            echo "Unknown Linux distribution: $DISTRO"
            exit 1
        fi
    elif [[ "$OS" == "macos" ]]; then
        echo "Installing Python via Homebrew..."
        if ! command -v brew &> /dev/null; then
            echo "Homebrew not found. Please install from https://brew.sh"
            exit 1
        fi
        brew install python@3.12
    elif [[ "$OS" == "windows" ]]; then
        echo "Please download and install Python from https://www.python.org/downloads/"
        echo "Make sure to check 'Add Python to PATH' during installation"
        exit 1
    fi
    
    echo "✓ Python installed successfully"
}

# ---------------------------------------------------------------------------
# Install uv (and uvx) if missing
# ---------------------------------------------------------------------------
install_uv() {
    echo ""
    echo "Installing uv (Python package manager)..."

    if [[ "$OS" == "linux" ]] || [[ "$OS" == "macos" ]]; then
        echo "Installing uv via official installer..."
        if command -v curl &> /dev/null; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
        elif command -v wget &> /dev/null; then
            wget -qO- https://astral.sh/uv/install.sh | sh
        else
            # Fallback: install via pip
            echo "curl/wget not found — attempting pip install uv..."
            $PYTHON_CMD -m pip install --user uv
        fi
        # Reload PATH so uv is found in this session
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    elif [[ "$OS" == "windows" ]]; then
        echo "On Windows, please install uv from https://github.com/astral-sh/uv"
        echo "Or run: pip install uv"
    fi

    if command -v uv &> /dev/null; then
        echo "✓ uv installed successfully"
    else
        echo "[WARN] uv could not be installed automatically. Some features may not work."
        echo "  Install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
}

check_uvx() {
    # Reload PATH to pick up ~/.local/bin, ~/.cargo/bin where uv is commonly installed
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv &> /dev/null; then
        echo "⚠ uv/uvx not found. Attempting to install..."
        install_uv
    fi

    if command -v uv &> /dev/null; then
        UV_VERSION=$(uv --version 2>/dev/null | head -1)
        echo "✓ $UV_VERSION found (uvx available)"
    else
        echo "[WARN] uv/uvx not available — install from https://astral.sh/uv"
    fi
}

# ---------------------------------------------------------------------------
# Check and validate requirements
# ---------------------------------------------------------------------------

# Returns true (0) if the given python command is >= 3.11
_python_meets_minimum() {
    local cmd="$1"
    command -v "$cmd" &> /dev/null || return 1
    local major minor
    major=$("$cmd" -c 'import sys; print(sys.version_info.major)' 2>/dev/null)
    minor=$("$cmd" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null)
    [[ -n "$major" && -n "$minor" ]] || return 1
    [[ "$major" -gt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -ge 11 ]]; }
}

# Scans well-known python command names and sets PYTHON_CMD to the first one >= 3.11
_find_python_cmd() {
    PYTHON_CMD=""
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if _python_meets_minimum "$cmd"; then
            PYTHON_CMD="$cmd"
            return 0
        fi
    done
    return 1
}

check_python() {
    if ! _find_python_cmd; then
        echo "⚠ Python 3.11+ not found. Attempting to install..."
        install_python

        if ! _find_python_cmd; then
            echo "✗ Failed to install Python 3.11+ automatically."
            echo "Please manually install Python 3.11 or higher."
            if [[ "$OS" == "linux" ]] && [[ "$DISTRO" == "ubuntu" ]]; then
                echo ""
                echo "For Ubuntu, you may need to add the deadsnakes PPA:"
                echo "  sudo add-apt-repository ppa:deadsnakes/ppa"
                echo "  sudo apt update"
                echo "  sudo apt install python3.11 python3.11-venv"
            elif [[ "$OS" == "macos" ]]; then
                echo ""
                echo "For macOS, you can use Homebrew:"
                echo "  brew install python@3.11"
            fi
            exit 1
        fi
    fi

    PYTHON_VERSION=$("$PYTHON_CMD" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    echo "✓ Python $PYTHON_VERSION found ($PYTHON_CMD)"
    export PYTHON_CMD
}

# Returns true (0) if the given node command is >= 20.9.0
_node_meets_minimum() {
    local cmd="$1"
    command -v "$cmd" &> /dev/null || return 1
    local version_str
    version_str=$("$cmd" -v 2>/dev/null | sed 's/^v//')
    if [[ -z "$version_str" ]]; then return 1; fi
    
    # Simple semantic version comparison
    local major minor patch
    IFS='.' read -r major minor patch <<< "$version_str"
    
    if [[ "$major" -gt 20 ]]; then
        return 0
    elif [[ "$major" -eq 20 ]]; then
        if [[ "$minor" -ge 9 ]]; then
            return 0
        fi
    fi
    return 1
}

check_node() {
    if ! command -v node &> /dev/null; then
        echo "⚠ Node.js not found. setup.py will attempt to install it."
        return 0
    fi
    
    local node_ver
    node_ver=$(node -v | sed 's/v//')
    local major=$(echo $node_ver | cut -d. -f1)
    local minor=$(echo $node_ver | cut -d. -f2)
    
    if [[ "$major" -lt 20 ]] || { [[ "$major" -eq 20 ]] && [[ "$minor" -lt 9 ]]; }; then
        echo "⚠ Node.js version $node_ver is too old (>= 20.9.0 required)."
        echo "  setup.py will attempt to update it."
    else
        echo "✓ Node.js $node_ver found"
    fi
}

check_git() {
    if ! command -v git &> /dev/null; then
        echo "⚠ git not found."
        install_git
    fi
    echo "✓ git found"
}

# ---------------------------------------------------------------------------
# Main setup flow
# ---------------------------------------------------------------------------
main() {
    echo ""
    echo "========================================================"
    echo "   Synapse AI — Repository Setup"
    echo "========================================================"
    echo ""
    
    detect_os
    check_python
    check_node
    check_uvx
    
    # OS-specific installation directory
    if [[ "$OS" == "macos" ]]; then
        INSTALL_DIR="$HOME/Library/Application Support/SynapseAI"
    else
        INSTALL_DIR="$HOME/.local/share/SynapseAI"
    fi
    MARKER_FILE="$INSTALL_DIR/.installed"

    # Already installed check
    if [ -f "$MARKER_FILE" ]; then
        echo ""
        echo "======================================================"
        echo -e "\033[92m   Synapse AI is already installed!\033[0m"
        echo "======================================================"
        echo ""
        echo -e "   \033[96mLocation: $INSTALL_DIR\033[0m"
        echo ""

        # Delegate to 'synapse upgrade' — it handles stop, download, rebuild
        export PATH="$INSTALL_DIR/bin:$PATH"
        echo ""
        echo -e "\033[96m==> Running synapse upgrade...\033[0m"
        "$INSTALL_DIR/bin/synapse" upgrade

        echo ""
        echo "======================================================"
        echo -e "\033[92m   Synapse AI has been updated!\033[0m"
        echo "======================================================"
        echo ""
        echo "To start Synapse:"
        echo -e "  \033[96msynapse start\033[0m"
        echo ""
        echo "Other commands:"
        echo "  synapse stop      -- stop running services"
        echo "  synapse status    -- check service status"
        echo "  synapse restart   -- restart services"
        echo ""

        # Auto-activate PATH in this terminal without requiring the user to source ~/.bashrc
        if [ "$_SOURCED" = false ] && [ -t 1 ] && [ -f "$HOME/.bashrc" ]; then
            echo -e "\033[96m==> Activating 'synapse' command in this terminal...\033[0m"
            exec bash --rcfile "$HOME/.bashrc" -i
        fi
        exit 0
    fi

    # Download latest release from GitHub (no git required)
    GITHUB_API="https://api.github.com/repos/naveenraj-17/synapse-ai/releases/latest"
    echo ""
    echo -e "\033[96mFetching latest Synapse AI release...\033[0m"
    if command -v curl &> /dev/null; then
        TARBALL_URL=$(curl -s "$GITHUB_API" | grep '"tarball_url"' | head -1 | cut -d'"' -f4)
    elif command -v wget &> /dev/null; then
        TARBALL_URL=$(wget -qO- "$GITHUB_API" | grep '"tarball_url"' | head -1 | cut -d'"' -f4)
    else
        TARBALL_URL=""
    fi

    mkdir -p "$INSTALL_DIR"
    if [ -n "$TARBALL_URL" ]; then
        echo -e "\033[96mDownloading release...\033[0m"
        if command -v curl &> /dev/null; then
            curl -L "$TARBALL_URL" | tar -xz -C "$INSTALL_DIR" --strip-components=1
        else
            wget -qO- "$TARBALL_URL" | tar -xz -C "$INSTALL_DIR" --strip-components=1
        fi
        echo -e "\033[92m[OK] Release downloaded and extracted.\033[0m"
    else
        echo -e "\033[93m[WARN] Could not fetch release from GitHub. Falling back to git clone...\033[0m"
        if ! command -v git &> /dev/null; then
            echo "git not found and GitHub download failed. Please install git or check your internet connection."
            exit 1
        fi
        git clone https://github.com/naveenraj-17/synapse-ai.git "$INSTALL_DIR"
    fi
    
    cd "$INSTALL_DIR"
    
    echo ""
    if [ -t 1 ]; then
        $PYTHON_CMD setup.py < /dev/tty
    else
        $PYTHON_CMD setup.py
    fi

    # Add bin dir to profile if missing
    BIN_DIR="$INSTALL_DIR/bin"
    for profile in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.bash_profile"; do
        if [ -f "$profile" ]; then
            if ! grep -q "$BIN_DIR" "$profile"; then
                echo "" >> "$profile"
                echo "# Synapse AI" >> "$profile"
                echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$profile"
                echo -e "\033[92m[OK] Added Synapse to profile ($profile)\033[0m"
            fi
        fi
    done

    # Activate the new PATH in the current shell session
    export PATH="$BIN_DIR:$PATH"

    echo ""
    echo "========================================================"
    echo -e "\033[92m   Synapse AI setup complete!\033[0m"
    echo -e "   Installed at:  \033[96m$INSTALL_DIR\033[0m"
    echo ""
    echo -e "   To start Synapse:"
    echo -e "     \033[96msynapse start\033[0m"
    echo ""
    echo -e "   To upgrade in the future:"
    echo -e "     \033[96msynapse upgrade\033[0m"
    echo "========================================================"

    # Auto-activate PATH in this terminal without requiring the user to source ~/.bashrc
    if [ "$_SOURCED" = false ] && [ -t 1 ] && [ -f "$HOME/.bashrc" ]; then
        echo ""
        echo -e "\033[96m==> Activating 'synapse' command in this terminal...\033[0m"
        exec bash --rcfile "$HOME/.bashrc" -i
    fi
}

main

