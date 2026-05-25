# Neovim Config

## Install

```sh
cp -r init.lua lua/ lazy-lock.json ~/.config/nvim/
```

First launch will auto-install [lazy.nvim](https://github.com/folke/lazy.nvim) and all plugins. `lazy-lock.json` pins exact plugin versions.

## Requirements

- **Neovim >= 0.11** — uses `vim.lsp.config()` native LSP API
- **clangd** — C LSP server (code navigation, hover, diagnostics)
- **fd** (`fdfind` on Debian/Ubuntu) — Telescope find_files backend
- **git** — lazy.nvim plugin management, Gitsigns
- **A Nerd Font** — icons in lualine, nvim-tree, etc.

### Quick install (Debian/Ubuntu)

```sh
sudo apt install clangd fd-find git
```

### Nerd Font

Download from [nerdfonts.com](https://www.nerdfonts.com/), e.g. JetBrains Mono:

```sh
mkdir -p ~/.local/share/fonts
curl -fLo ~/.local/share/fonts/JetBrainsMonoNerdFont-Regular.ttf \
  https://github.com/ryanoasis/nerd-fonts/raw/master/patched-fonts/JetBrainsMono/Ligatures/Regular/JetBrainsMonoNerdFont-Regular.ttf
fc-cache -fv
```

Then set your terminal font to the Nerd Font.
