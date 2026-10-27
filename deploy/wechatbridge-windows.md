# Windows Deployment

## Quick Start

Install with [pipx](https://pypa.github.io/pipx/) (requires Python >= 3.10):

```cmd
py -m pip install --user pipx
py -m pipx ensurepath
pipx install wechatbridge-cli
```

Configure (create `%USERPROFILE%\.config\wechatbridge\.env`, see `deploy\wechatbridge.env.example` for all options), then run:

```cmd
wechatbridge
```

## Autostart via Task Scheduler

1. Open Task Scheduler -> Create Task
2. Action: Start a program -> `%USERPROFILE%\.local\bin\wechatbridge.exe` (the pipx-installed entry point)
3. Trigger: At log on
4. Settings: Restart on failure, every 1 minute, up to 3 times

## Upgrade

```cmd
pipx upgrade wechatbridge-cli
```

Config lives under `%USERPROFILE%\.config\wechatbridge\` and data under `%USERPROFILE%\.local\share\wechatbridge\`, so upgrades never touch either.

## Notes

- Default instance data root: `%USERPROFILE%\.local\share\wechatbridge\<instance>\`  
  (`WECHATBRIDGE_INSTANCE` defaults to `default`)
- Default session directory: `%USERPROFILE%\.local\share\wechatbridge\<instance>\sessions`
- State / QR paths also live under that instance root unless overridden by env
- Multi-instance: run separate processes with different `WECHATBRIDGE_INSTANCE` values
- Subprocess env sets `HOME` and `USERPROFILE` to the **per-user session dir** (agy, grok, and codex)
- `os.chmod` is a no-op on Windows; token file permissions rely on NTFS ACLs
- `os.setsid` (Unix process group) is not used on Windows; CLI subprocesses inherit the parent's process group
