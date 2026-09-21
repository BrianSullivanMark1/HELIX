# HELIX on Kate's PC - Monday morning, in order

Fifteen minutes if the installers are quick. Everything runs under **her own** logins (gcloud,
GitHub, Claude); nothing of yours is copied over. Do these as her, on her PC.

## 1. The tools (once)

1. **Python 3.12** - python.org, tick "Add python.exe to PATH".
2. **Node 20 LTS** - nodejs.org (gives `npm`, needed to build the face and for the Firebase CLI).
3. **Git** - git-scm.com, defaults.
4. **Google Cloud CLI** - cloud.google.com/sdk. Then, in a new terminal:
   `gcloud auth login` (her mark1online.com account) and `gcloud config set project windy-celerity-392822`.
5. **Firebase CLI** - `npm install -g firebase-tools` then `firebase login` (same account).
6. **Claude Code** - `npm install -g @anthropic-ai/claude-code`, then `claude` once to sign in, then
   `claude setup-token` and keep the token it prints (step 5 below).

## 2. The code

```
cd %USERPROFILE%\Desktop
git clone https://github.com/BrianSullivanMark1/HELIX.git
cd HELIX
git checkout mark1
pip install -r requirements.txt edge-tts
cd web && npm install && npm run build && cd ..
```

Also clone the console (dev.ps1 lives here; HELIX ships through it):

```
cd %USERPROFILE%\Desktop
git clone https://github.com/BrendanSullivanMark1/BRMS_MES_WEB_APP.git
```

## 3. Start it

`python main.py` from the HELIX folder (or double-click `install-shortcut.bat` once and use the
Desktop icon). Wait for the boot screen to finish.

## 4. Settings (the menu glows until these are green)

1. **The brain**: paste the Claude token from `claude setup-token`.
2. **The Board > GitHub**: a classic token from github.com/settings/tokens with the `repo` scope only.
3. **The Board > The tools on this PC**: press Check again. Fix any amber row; set the console
   checkout to `...\Desktop\BRMS_MES_WEB_APP\BRMS_MES_WEB_VERSION` (the folder that holds `dev.ps1`).
4. **Radio** (the gear): the bucket name `helix-radio-mark1`.

## 5. Her rights

She is on the production allowlist already (`kate@mark1online.com` in `helix/domain/fleet.py` -
confirm her real address; change that one line if it differs). Production still asks her to type
`deploy prod` and confirm, under her own gcloud login, with an audit row written first.

## 6. First deploy check

Console > ECHO > Deploy > DEV > Ship. Watch it in CURRENT TASKS. If it ends red, click the bar,
"Copy for debugging", and send the text.
