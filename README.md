# Permanent

本地编程助手，命令是 `perm`。在项目目录里打开终端界面，读改文件、跑命令、查网页，对话记在本机。仓库是 [perm-agent](https://github.com/wangyin717/perm-agent)。

当前版本 `v0.3.0`。

它对着当前目录干活。一次对话写在 `~/.permanent` 里，换一个项目就是另一份会话。也能操作你已经打开、并且登录过的 Chrome，用来点页面、填表、截图。公开网页用搜索和抓取。

本机需要 `git` 和 `curl`。浏览器功能需要本机的 Google Chrome。第一次连上时，Chrome 会问是否允许远程调试，点 Allow 之后这一步会自己继续。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

脚本安装最新的 `vX.Y.Z`，数据放在 `~/.permanent`，命令放在 `~/.local/bin/perm`。macOS 的 zsh 会把这一行写进 `~/.zshrc`。当前终端还找不到 `perm` 时，先执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

第一次安装会问 DeepSeek 的密钥。也可以自己写到 `~/.permanent/.env`：

```bash
DEEPSEEK_API_KEY=sk-...
```

然后进入项目目录：

```bash
perm
```

指定这一版：

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash -s -- --ref v0.3.0
```

已经装过的，更新到最新 tag：

```bash
perm update
```

跳过浏览器命令：

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash -s -- --no-browser-use
```

装好之后：

- 代码和虚拟环境在 `~/.permanent/src`
- 会话、记忆、密钥在 `~/.permanent`
- 浏览器命令的独立环境在 `~/.permanent/uv-tools`
- 托管安装如果发现更新的 tag，启动时会提示运行 `perm update`

界面里输入 `/help` 看命令。

## 从源码跑

```bash
uv sync
uv run perm
```

`uv run perm -s wy1 "写一段 python 并运行"` 只跑一轮，不开界面。不要设置 `PYTHONPATH`。密钥放仓库根 `.env` 或 `~/.permanent/.env`。

循环和工具的细节在 `docs/harness.md`。
