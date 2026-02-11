# GSM-OBS Shim

WARNING: VIBECODED SOFTWARE

This application has been purely vibecoded and should not be trusted for any mission-critical purpose. It was built specifically for one person's setup and is not intended to be a well-supported application for end users.

## System Requirements / Hardcoded Constraints

This project is tailored specifically for:
- OS: Linux
- Desktop Environment: KDE Plasma
- Display Protocol: Wayland
- Specifics: It relies on XDG Portals for window selection and GSTreamer for pipewire capture.

If you are not the creator, this will almost certainly break on your machine without modification.

## What is this?

This is a shim/proxy that sits between the GameSentenceMiner (GSM) client and OBS. It allows for:
1.  Mock Mode (Default): Emulates an OBS WebSocket server, returning fake scenes and screenshots (optionally captured from a real window via XDG Portal).
2.  Proxy Mode: Transparently passes traffic between the GSM client and a real OBS instance running on port 7275.

## Key Features

- Dynamic GUI: A small tkinter window allows you to update the "Game Name" reported to GSM at runtime.
- Auto Window Selection: In Mock Mode, it automatically triggers the KDE/Wayland window selection portal.
- Persistence: Remembers your preferred game name between runs (stored in ~/.config/gsm-obs-shim/config.json).
- Screenshot Resizing: Supports imageWidth and imageHeight parameters in GetSourceScreenshot requests using Pillow.

## Installation

This application should be run within a Python virtual environment.

1. Create and activate a virtual environment.
2. Ensure you have the "tk" (Tkinter) interface for Python installed in your environment.
3. Install the remaining requirements:
```bash
pip install -r requirements.txt
```

## Running

All commands should be executed within your virtual environment:
- Mock Mode: python gsm_obs_shim.py
- Proxy Mode: python gsm_obs_shim.py --proxy

## Contributing / Usage
You are free to use this code as an example or inspiration for your own implementation. No support will be provided. Use at your own risk.
