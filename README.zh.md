<div align="center">
  <img src="docs/logo.jpg" alt="Permanent" width="280">
</div>

<div align="center">
  <a href="README.zh.md">中文</a> · <a href="README.md">English</a>
</div>

# Permanent

这是一个 coding agent，也能处理日常问题，因为它能用浏览器，也能操作电脑。

## 核心能力

- 在当前项目里读改文件、跑命令。
- 打开网页，处理需要点击、登录或页面脚本的操作。
- 操作电脑上的普通软件。
- 搜索网页，阅读 PDF、文档和图片。

## 快速开始

### 安装

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

Windows 请先打开 WSL，再在里面执行这条命令。

### 环境配置

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 运行

```bash
perm
```

进入后用 `/login` 配置模型。推荐 DeepSeek。

网页搜索默认用 DuckDuckGo。想要更好的结果，在 `/login` 的 Web search 里填写 `PERPLEXITY_API_KEY`。

## 常用命令

在输入框里以 `/` 开头。只输入 `/` 会列出命令，按 Tab 补全。

- `/login`：配置模型和 API key。
- `/model`：切换模型。
- `/new`：新开一个会话，历史是空的。
- `/resume`：列出当前项目的会话，点一行进入。也可以 `/resume 2`，或 `/resume` 加上会话 id。
- `/rename 标题`：给当前会话起名。`/rename --auto` 用第一条提问做标题。
- `/session`：显示会话 id 和日志路径。
- `/copy`：复制最近一条回复，不含思考过程。
- `/theme`：切换日间或夜间。
- `/help`：列出这些命令。
- `/exit`：退出。`/quit` 一样。

Enter 发送。Esc 中止当前这一轮。Ctrl+Q 退出。
