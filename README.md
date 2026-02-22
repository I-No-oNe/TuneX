# 🎵 Music Player Server - Setup Guide


> [!NOTE]
> This project is designed to run on Linux-based systems, such as a Raspberry Pi. It provides a web interface to control music playback on the device.
> (and then you can use port forwarding to access it from outside your local network if needed)


## Install

```bash
# 1. Copy the music-server folder to your Pi, then:
cd music-server

# 2. Install dependencies
pip install -r requirements.txt

# 3. Change the default API key
python manage_keys.py gen yourname
# → note the generated key, you'll enter it in the browser

# 4. Run the server
python server.py
```

## Access the player

Open a browser on ANY device on your local network:

```
http://<local-ip>:8080
```

Enter your API key → Connect → enjoy!

## Find your IP address:

```bash
hostname -I
```

## Run as a background service (auto-start on boot)

Create `/etc/systemd/system/pimusic.service`:

```ini
[Unit]
Description = Music Server
After = network.target

[Service]
WorkingDirectory = /home/user/music-server
ExecStart = /usr/bin/python3 server.py
Restart = always
User = user

[Install]
WantedBy = multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable pimusic
sudo systemctl start pimusic
```

## Manage users

```bash
python manage_keys.py list              # show all users
python manage_keys.py gen alice         # add user with random key
python manage_keys.py add bob mykey123  # add user with custom key
python manage_keys.py remove alice      # remove user
```


## Ty for using this project, Don't forget to star ⭐ if you like it!