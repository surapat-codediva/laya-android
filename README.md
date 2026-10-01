# laya-android

An agent skill (Claude Code and other [skills](https://github.com/vercel-labs/skills)-compatible agents)
that controls an Android phone or emulator over `adb`: read the screen, tap, type, swipe, and
navigate toward a goal. It uses the local [Laya](https://huggingface.co/convaiinnovations/laya)
classifier for cheap decisions (which element to tap, yes/no screen checks), and the agent takes
over whenever Laya is unsure.

## Install

```bash
npx skills add surapat-codediva/laya-android          # project-level
npx skills add surapat-codediva/laya-android -g       # user-level (~/.claude/skills)
```

## Requirements

- `adb` on PATH (Android platform-tools), with a device connected over USB or wireless debugging
- `python3` (set `LAYA_PYTHON` to pick a specific interpreter)

The first time the skill runs, `la` creates a `.venv` inside the skill folder and installs
`laya` (pulls in torch). The first Laya call then downloads the model (~650 MB).

## Usage

Ask your agent to do something on the phone, e.g. "open Settings on my Android and turn on
dark mode". Or run the CLI directly:

```bash
bash skills/laya-android/la screen              # list tappable elements
bash skills/laya-android/la tap 3               # tap element 3
bash skills/laya-android/la run "open Wi-Fi settings" --done "Is the Wi-Fi settings page shown?"
```

See [`skills/laya-android/SKILL.md`](skills/laya-android/SKILL.md) for all commands, the
reliability notes (zero-shot Laya is close to random — the agent verifies), and safety rules.

## License

MIT. Laya itself is Apache-2.0.
