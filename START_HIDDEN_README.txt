Hidden bot launcher

Use start_hidden.vbs to run the bot without a visible command prompt window.

The launch chain is:
start_hidden.vbs -> rule34.bat -> python bot.py

Keep using rule34.bat inside the chain because it handles /restart by watching
for bot.py exit code 42 and starting it again.
