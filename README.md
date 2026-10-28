# Telegram media bot

Python bot based on aiogram with a Flask webhook endpoint. The Docker image includes `ffmpeg` and starts the app with Gunicorn.

For Render Free deployment steps and known limitations, see [README-Render.md](README-Render.md).

Never commit a real `.env` file, Telegram bot token, `.data` directory, SQLite database, or logs. Add the token only as a private environment variable in Render.
