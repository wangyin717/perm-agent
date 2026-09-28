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

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

Windows 请先打开 WSL，再在里面执行这条命令。

进入项目目录后运行 `perm`。第一次打开时填写 `DEEPSEEK_API_KEY`。模型推荐用 DeepSeek，之后也可以用 `/login` 更换。

网页搜索默认用 DuckDuckGo。想要更好的结果，在 `/login` 的 Web search 里填写 `PERPLEXITY_API_KEY`。
