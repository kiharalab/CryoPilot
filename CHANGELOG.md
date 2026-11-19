# Changelog

What changed in each CryoPilot release. The website and the app show these notes
as plain text, so write them for the people who use CryoPilot, without Markdown.

## 0.3.7 - 2026-11-10

- New: Settings has an Experimental section. Turn on experimental features to let new reconstruction sessions search the web, search papers in Europe PMC, and read public web pages, for example the CryoSPARC guide or a methods section. Neither service needs an account, and the run report notes when a session used them.

## 0.3.6 - 2026-11-09

- Each session has a run report: the final map or model, its FSC or validation numbers, the jobs and parameters that led to it, the software versions, the cost, and a draft Methods paragraph. You can also download the whole conversation.
- CryoPilot can alert you through a webhook or by email when a run finishes, asks a question, fails, or pauses. Set it up in Settings, under Alerts.
- The message box shows what a session has cost so far, estimated from list prices. A spending limit pauses a run before its next model request, and you can raise it to continue.
- The status line now says what a run is waiting for, including which CryoSPARC jobs are queued or running and for how long.
- The sidebar stays up to date for sessions that are not open, and an agent that crashes now shows as an error.

## 0.3.5 - 2026-11-04

- CryoPilot tells you when a newer version is available and shows what changed.
- Update with one click in the app, or run cryopilot update in a terminal. Your projects, sessions, and settings stay as they are.
- An update checks the download before it changes anything, and a failed update leaves your current version as it was.
- Updates and the installer remove the files of older versions, so they no longer fill your disk. The installer now keeps the program as cryopilot-linux-x86_64.

## 0.3.4 - 2026-11-03

- First-time setup lets you choose an OpenAI or an Anthropic API key.
- Notes that a Claude model writes before it uses a tool no longer appear in its final answer.

## 0.3.3 - 2026-11-02

- The modeling agent can run on Claude models.

## 0.3.2 - 2026-11-02

- Testing an OpenAI key no longer fails with a certificate error on Ubuntu and Debian.

## 0.3.1 - 2026-10-30

- Continue from a CryoSPARC project that a person or an earlier session already started.
- GPT-6 models no longer reject large images.

## 0.3.0 - 2026-10-30

- The reconstruction agent can run on Claude models.
- Choose from the latest Claude and GPT models.
- The app works on phones.

## 0.2.0 - 2026-10-27

- CryoPilot is released under the PolyForm Noncommercial license, which you accept when you first start it.
