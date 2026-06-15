# Deploying as systemd services

Copy the units, reload, and enable. The SDR unit is a template — instantiate it
per dongle **by serial** (`python -m tmt.devices` to list serials):

```bash
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tmt-ble.service tmt-alertd.service tmt-api.service
sudo systemctl enable --now tmt-sdr@<serial>.service      # one per dongle

# push secrets (optional):
echo 'TMT_NTFY_TOPIC=your-secret-topic' | sudo tee /etc/track_my_tracker/alertd.env
sudo systemctl restart tmt-alertd
```

Paths in the units assume the repo at `/home/pi/Track_My_Tracker` with a venv at
`.venv`. Adjust `WorkingDirectory`/`ExecStart` if you install elsewhere.
