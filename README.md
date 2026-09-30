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

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

On Windows, open WSL first, then run this command there.

### Setup

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Run

```bash
perm
```

After it opens, use `/login` to configure a model. DeepSeek is the recommended provider.

Web search uses DuckDuckGo unless you set a key. For better results, add `PERPLEXITY_API_KEY` under Web search in `/login`.

## Commands

Type `/` in the input box. `/` alone lists the commands. Tab completes one.

- `/login`: configure the model and API keys.
- `/model`: switch the model.
- `/new`: start a session with an empty history.
- `/resume`: list sessions in the current project. Click a row to open it. `/resume 2` or `/resume` plus a session id also works.
- `/rename title`: name the current session. `/rename --auto` uses the first prompt as the title.
- `/session`: show the session id and the log path.
- `/copy`: copy the latest reply, without the thinking text.
- `/theme`: switch between day and night.
- `/help`: list these commands.
- `/exit`: quit. `/quit` does the same.

Enter sends. Esc aborts the current turn. Ctrl+Q quits.
