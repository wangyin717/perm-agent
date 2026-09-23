<p align="center">
  <img src="docs/logo.jpg" alt="Permanent" width="360">
</p>

<p align="center">
  <a href="README.md">中文</a> · <a href="README.en.md">English</a>
</p>

# Permanent

在当前项目目录里干活的本地助手。打开终端界面，读改文件、跑命令、查网页。对话记在本机。

当前版本 `v0.4.0`。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

这一行安装最新版本，命令放在 `~/.local/bin/perm`。当前窗口还找不到 `perm` 时，先执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

然后进入项目目录，运行 `perm`。

本机需要 `git` 和 `curl`。第一次会问 DeepSeek 的密钥。也可以写到 `~/.permanent/.env`：

```bash
DEEPSEEK_API_KEY=sk-...
```

已经装过的，更新到最新版本：

```bash
perm update
```

只要这一版：

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash -s -- --ref v0.4.0
```

## 能做什么

- 读、改当前目录里的文件，运行命令
- 搜索公开网页，打开页面
- 用本机已经登录的 Chrome 点页面、填表、截图。第一次连上时，Chrome 会问是否允许远程调试，点 Allow
- 本机有 `cua-driver` 时，可以操作桌面上的应用
- 会改文件或跑命令时先问一声。可以允许一次、允许同类，或拒绝

会话、记忆和密钥在 `~/.permanent`。换一个项目目录就是另一份会话。界面里输入 `/help` 看命令。

不要浏览器功能：

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash -s -- --no-browser-use
```

## 从源码跑

```bash
uv sync
uv run perm
```

`uv run perm -s wy1 "写一段 python 并运行"` 只跑一轮，不开界面。密钥放仓库根 `.env` 或 `~/.permanent/.env`。

实现细节在 `docs/harness.md`。
