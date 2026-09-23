<p align="center">
  <img src="docs/logo.jpg" alt="Permanent" width="360">
</p>

<p align="center">
  <a href="README.md">中文</a> · <a href="README.en.md">English</a>
</p>

# Permanent

A local assistant for the project directory you are in. It opens a terminal UI, reads and edits files, runs commands, and looks up the web. The conversation stays on this machine.

Current release: `v0.4.0`.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
```

That installs the latest release and puts the command at `~/.local/bin/perm`. If this window cannot find `perm` yet, run:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then `cd` into a project and run `perm`.

You need `git` and `curl`. The first install asks for a DeepSeek API key. You can also write it yourself in `~/.permanent/.env`:

```bash
DEEPSEEK_API_KEY=sk-...
```

Already installed? Update to the latest release:

```bash
perm update
```

Install this release only:

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash -s -- --ref v0.4.0
```

## What it does

- Read and edit files in the current directory, and run commands
- Search the public web and open pages
- Use the Chrome you are already logged into: click, fill forms, take screenshots. The first connection asks Chrome for remote debugging; click Allow
- Drive desktop apps when `cua-driver` is already installed on this machine
- Ask before it edits a file or runs a command. You can allow once, allow similar requests, or deny

Sessions, memory, and the key live in `~/.permanent`. A different project directory is a different session. Type `/help` in the UI for the command list.

Skip the browser tools:

```bash
curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash -s -- --no-browser-use
```

## Run from source

```bash
uv sync
uv run perm
```

`uv run perm -s wy1 "write a python snippet and run it"` runs one turn and does not open the UI. Put the key in `.env` at the repo root, or in `~/.permanent/.env`.

How the loop and tools work is in `docs/harness.md`.
