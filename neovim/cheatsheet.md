# Neovim Cheat Sheet

Leader = `Space`

## 读代码核心流程

跳进去，跳回来。这是最重要的两个操作：

| Key | Action |
|-----|--------|
| `gd` | 跳到定义（LSP） |
| `Ctrl-o` | **跳回**（jump list 后退） |
| `Ctrl-i` | 跳前（jump list 前进） |

读内核时反复 `gd` → `Ctrl-o` 就能在调用链上自由穿梭。

## 文件 / 符号查找 — Telescope

| Key | Action |
|-----|--------|
| `Space f f` | 按文件名查找 |
| `Space f g` | 全局 grep（找函数/宏定义利器） |
| `Space f b` | 切换已打开的 buffer |
| `Space f s` | 当前文件的符号列表（函数、结构体、宏） |
| `Space f /` | 当前 buffer 内模糊搜索 |

Telescope 内操作：`Ctrl-n/p` 上下移动，`Enter` 打开，`Esc` 退出。

## 快速切换文件 — Harpoon

把常看的几个文件钉住，一键切换，不用每次搜索：

| Key | Action |
|-----|--------|
| `Space h a` | 把当前文件加入 harpoon |
| `Space h h` | 打开 harpoon 列表 |
| `Space 1-4` | 直接跳到第 1-4 个文件 |

读内核时把当前关注的 2-3 个文件 harpoon 起来，效率提升明显。

## 文件树 — NvimTree

| Key | Action |
|-----|--------|
| `Space e` | 打开/关闭文件树 |

文件树内：`Enter` 打开，`a` 新建文件，`d` 删除，`r` 重命名，`q` 关闭。

## Git — Gitsigns

| Key | Action |
|-----|--------|
| `]h` / `[h` | 下一个/上一个改动块 |
| `Space g p` | 预览当前改动 |
| `Space g b` | 当前行 blame（谁改的、什么时候） |

## LSP

| Key | Action |
|-----|--------|
| `K` | 悬浮文档（光标下符号的类型/注释） |
| `Space d` | 显示当前行诊断信息 |
| `]d` / `[d` | 下一个/上一个诊断 |
| `gr` | 查找所有引用 |

## 补全 — nvim-cmp

插入模式下自动弹出，不需要额外触发：

| Key | Action |
|-----|--------|
| `Ctrl-n` / `Ctrl-p` | 选择下/上一个补全项 |
| `Ctrl-y` | 确认补全 |
| `Ctrl-Space` | 手动触发补全 |

## 内置 — 值得记住的

| Key | Action |
|-----|--------|
| `*` | 搜索光标下的单词（找所有使用处） |
| `Ctrl-w s` | 水平分屏 |
| `Ctrl-w v` | 垂直分屏 |
| `Ctrl-w h/j/k/l` | 在分屏间移动 |
| `Ctrl-\` | 打开/关闭浮动终端 |
| `:bd` | 关闭当前 buffer |
| `u` | 撤销 |
| `Ctrl-r` | 重做 |
