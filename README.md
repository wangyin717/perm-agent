<div align="center">
  <img src="docs/logo.jpg" alt="Permanent" width="280">
</div>

<div align="center">
  <a href="README.zh.md">中文</a> · <a href="README.md">English</a>
</div>

# Permanent

A coding agent that also handles everyday tasks, because it can browse the web and use the computer.

## Core capabilities

- Read and edit files, and run commands in the current project.
- Browse the web when a page needs clicks, login, or JavaScript.
- Use the desktop to drive native apps.
- Search the web, and read PDFs, documents, and images.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

On Windows, open WSL first, then run this command there.

Open a project directory and run `perm`. On first launch, set `DEEPSEEK_API_KEY`. DeepSeek is the recommended model provider. You can change it later with `/login`.

Web search uses DuckDuckGo unless you set a key. For better results, add `PERPLEXITY_API_KEY` under Web search in `/login`.
