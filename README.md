<div align="center">
  <img src="docs/logo.jpg" alt="Permanent" width="280">
</div>

<div align="center">
  <a href="README.zh.md">中文</a> · <a href="README.md">English</a>
</div>

# Permanent

A coding agent that also handles everyday tasks, because it can browse the web and use the computer.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

On Windows, open WSL first, then run this command there.

## Models

DeepSeek is the recommended provider. Set `DEEPSEEK_API_KEY` on first launch, or later with `/login`.

## Web search

Search uses DuckDuckGo unless you set a key. For better results, add `PERPLEXITY_API_KEY` under Web search in `/login`.
